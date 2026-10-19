"""抽卡概率模型测试（纯逻辑，不需要 AstrBot）。

运行：
    python3 tests/test_draw_probability.py
    # 或
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import random
import re
import tempfile
import unittest
from pathlib import Path

from stub_astrbot import install

install()  # 必须在 import main 之前

import main as M  # noqa: E402

WEIGHTS = {6: 3.0, 5: 5.0, 4: 8.0, 3: 4.0, 2: 2.0, 1: 1.0}
SHARES = {r: w / sum(WEIGHTS.values()) for r, w in WEIGHTS.items()}
TOL = 0.02  # 8000 抽下统计误差约 ±0.005，留足余量


def make_library(specs: dict[int, int]) -> Path:
    """按 {星级: 张数} 建一个临时图库目录。"""
    d = Path(tempfile.mkdtemp(prefix="quote_lib_"))
    for rarity, count in specs.items():
        for i in range(count):
            (d / f"{rarity}.q{i}.jpg").write_bytes(b"x")
    return d


def draw_many(drawer, n: int) -> list[str]:
    return [Path(drawer.next()).name for _ in range(n)]


def rarity_of(name: str) -> int:
    return int(name.split(".", 1)[0])


class RarityRateTest(unittest.TestCase):
    """星级出率必须等于权重占比（这是 v2.2 的核心承诺）。"""

    def test_rates_match_weights_without_dedup(self):
        random.seed(11)
        d = make_library({6: 2, 5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL,
                                   msg=f"{rarity}★ 出率 {got:.3f} != {expected:.3f}")

    def test_dedup_does_not_distort_rarity_rates(self):
        """去重窗口只影响"选哪张"，不应改变星级出率。"""
        random.seed(12)
        d = make_library({6: 2, 5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL,
                                   msg=f"{rarity}★ 出率 {got:.3f} != {expected:.3f}")

    def test_rates_ignore_image_count_per_rarity(self):
        """同样的权重，图库张数比例不同，出率也应相同（旧版是张数比例）。"""
        random.seed(13)
        d = make_library({6: 1, 5: 1, 4: 10, 3: 1, 2: 1, 1: 20})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL)

    def test_zero_weight_disables_rarity(self):
        random.seed(14)
        d = make_library({6: 2, 5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights={**WEIGHTS, 6: 0.0}, pity={})
        names = draw_many(drawer, 3000)
        self.assertEqual(sum(1 for n in names if rarity_of(n) == 6), 0)

    def test_all_zero_weights_fall_back_to_uniform(self):
        random.seed(15)
        d = make_library({6: 2, 5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights={r: 0.0 for r in WEIGHTS}, pity={})
        names = draw_many(drawer, 5000)
        for rarity in WEIGHTS:
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, 1 / 6, delta=TOL)

    def test_only_present_rarities_are_drawn(self):
        random.seed(16)
        d = make_library({1: 3})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 200)
        self.assertTrue(all(rarity_of(n) == 1 for n in names))


class DedupTest(unittest.TestCase):
    """短期去重：同星级内不重复，且永不至于抽不出图。"""

    def test_no_adjacent_repeat_when_rarity_has_multiple_images(self):
        random.seed(21)
        d = make_library({3: 2})
        drawer = M.QuoteDrawer(d, window=8, weights={3: 1.0}, pity={})
        names = draw_many(drawer, 4000)
        repeats = sum(1 for a, b in zip(names, names[1:]) if a == b)
        self.assertEqual(repeats, 0)

    def test_window_zero_allows_repeats(self):
        random.seed(22)
        d = make_library({3: 2})
        drawer = M.QuoteDrawer(d, window=0, weights={3: 1.0}, pity={})
        names = draw_many(drawer, 4000)
        repeats = sum(1 for a, b in zip(names, names[1:]) if a == b)
        self.assertGreater(repeats, 0)

    def test_single_image_rarity_cannot_avoid_repeat(self):
        """只有 1 张图的星级必然会重复，这是设计内的行为。"""
        random.seed(23)
        d = make_library({6: 1})
        drawer = M.QuoteDrawer(d, window=8, weights={6: 1.0}, pity={})
        names = draw_many(drawer, 100)
        self.assertTrue(all(n == names[0] for n in names))

    def test_small_library_never_runs_out(self):
        random.seed(24)
        d = make_library({1: 1, 2: 1})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={})
        self.assertTrue(all(n for n in draw_many(drawer, 500)))


class PityTest(unittest.TestCase):
    def test_pity_guarantees_max_gap(self):
        random.seed(31)
        d = make_library({6: 1, 5: 1, 4: 2, 3: 5, 2: 20})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={6: 10})
        rarities = [rarity_of(n) for n in draw_many(drawer, 4000)]
        pos = [i for i, r in enumerate(rarities) if r == 6]
        self.assertTrue(pos, "4000 抽里应当出现 6★")
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        self.assertLessEqual(max(gaps), 10, f"6★ 最大间隔 {max(gaps)} 超过保底阈值")

    def test_pity_threshold_of_one_hits_every_draw(self):
        random.seed(32)
        d = make_library({6: 1, 1: 5})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={6: 1})
        for _ in range(20):
            drawer.next()
            self.assertEqual(drawer.last_trace["pity"]["triggered"], 6)

    def test_pity_without_target_rarity_keeps_counter_then_fires(self):
        random.seed(33)
        d = make_library({1: 20, 3: 2})  # 没有 6★
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={6: 10})
        for _ in range(15):
            drawer.next()
        trace = drawer.last_trace
        self.assertIsNone(trace["pity"]["triggered"])
        self.assertIn(6, trace["pity"]["skipped"])
        self.assertEqual(trace["pity"]["since_used"][6], 10)  # 计数保留

        (d / "6.new.jpg").write_bytes(b"x")  # 补一张 6★
        path = drawer.next()
        self.assertEqual(Path(path).name, "6.new.jpg")  # 下一抽立即命中
        self.assertEqual(drawer.last_trace["pity"]["triggered"], 6)

    def test_pity_counter_resets_only_up_to_drawn_rarity(self):
        """抽到 N 星只清 <= N 星的计数，更高星级计数继续累积。"""
        random.seed(34)
        d = make_library({6: 1, 1: 50})
        drawer = M.QuoteDrawer(d, window=0, weights={6: 0.0, 1: 1.0}, pity={6: 7})
        # 权重 6★=0（不参与正常抽取），但保底仍会把它抽出来
        for _ in range(6):
            drawer.next()
            self.assertEqual(drawer.last_trace["pity"]["since_after"][6], drawer.last_trace["pity"]["since_used"][6])
        drawer.next()  # 第 7 抽达到阈值
        self.assertEqual(drawer.last_trace["pity"]["since_after"][6], 0)


class StateTest(unittest.TestCase):
    def test_state_file_contents(self):
        random.seed(41)
        d = make_library({1: 3})
        drawer = M.QuoteDrawer(d, window=4, weights=WEIGHTS, pity={6: 10})
        drawer.next()
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state), {"since", "recent", "stats"})
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["since"]["6"], 1)

    def test_legacy_order_json_is_compatible(self):
        random.seed(42)
        d = make_library({1: 3})
        legacy = {"order": ["1.q0.jpg", "1.q1.jpg", "1.q2.jpg"], "index": 2,
                  "since": {"6": 4}}
        (d / "order.json").write_text(json.dumps(legacy), encoding="utf-8")
        drawer = M.QuoteDrawer(d, window=4, weights=WEIGHTS, pity={6: 10})
        self.assertTrue(drawer.next())
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        self.assertEqual(state["since"]["6"], 5)  # 旧计数被继承并 +1

    def test_deleted_recent_entry_is_ignored(self):
        random.seed(43)
        d = make_library({1: 2})
        (d / "order.json").write_text(
            json.dumps({"since": {}, "recent": ["1.gone.jpg"]}), encoding="utf-8"
        )
        drawer = M.QuoteDrawer(d, window=4, weights=WEIGHTS, pity={})
        self.assertTrue(drawer.next())

    def test_empty_library(self):
        d = Path(tempfile.mkdtemp(prefix="quote_empty_"))
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={})
        self.assertIsNone(drawer.next())
        self.assertIsNone(drawer.last_trace)
        self.assertEqual(M.format_draw_trace(None), "")


class TraceTest(unittest.TestCase):
    def test_trace_reports_model_and_pity(self):
        random.seed(51)
        d = make_library({6: 1, 5: 1, 4: 2, 3: 5, 2: 20})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={6: 10})
        drawer.next()
        trace = drawer.last_trace
        for key in ("group", "total", "counts", "weights", "share", "window",
                    "rarity_by_weight", "rarity_final", "pool_size",
                    "candidates", "excluded", "pity", "final"):
            self.assertIn(key, trace)
        self.assertEqual(trace["total"], 29)
        self.assertAlmostEqual(sum(trace["share"].values()), 1.0, places=6)
        self.assertIn("triggered", trace["pity"])

    def test_trace_marks_pity_override(self):
        random.seed(52)
        d = make_library({6: 1, 1: 30})
        drawer = M.QuoteDrawer(d, window=0, weights={6: 0.0, 1: 1.0}, pity={6: 2})
        for _ in range(2):
            drawer.next()
        trace = drawer.last_trace
        self.assertEqual(trace["pity"]["triggered"], 6)
        self.assertEqual(trace["rarity_final"], 6)
        self.assertNotEqual(trace["rarity_by_weight"], 6)  # 权重本来抽不到 6★
        self.assertIn("出货计数", M.format_draw_trace(trace))

    def test_debug_lines_include_rates(self):
        """调试输出：标题 + 出货计数 + 各星级「实际(设定)概率」。"""
        random.seed(53)
        d = make_library({6: 1, 3: 2})
        weights = {6: 2, 5: 8, 4: 50, 3: 40, 2: 0, 1: 0}
        drawer = M.QuoteDrawer(d, window=0, weights=weights, pity={})
        for _ in range(4):
            drawer.next()
        lines = M.format_draw_trace(drawer.last_trace).splitlines()
        self.assertEqual(lines[0], "[语录调试]")
        self.assertTrue(lines[1].startswith("出货计数：6★"))
        self.assertTrue(lines[2].startswith("概率统计："))
        self.assertIn("6★", lines[2])
        self.assertIn(" / ", lines[1])
        self.assertIn(" / ", lines[2])
        self.assertIn("(2)%", lines[2])     # 6★ 设定概率
        self.assertIn("(40)%", lines[2])    # 3★ 设定概率
        self.assertNotIn("权重：", "\n".join(lines))
        self.assertEqual(M.format_draw_trace(None), "")

    def test_debug_rates_use_actual_counts(self):
        """实际概率 = 累计出货 / 总抽数；设定概率 = 权重占比。"""
        random.seed(54)
        d = make_library({1: 3})  # 只有 1★，权重只给 1★
        drawer = M.QuoteDrawer(d, window=0, weights={6: 0, 5: 0, 4: 0, 3: 0, 2: 0, 1: 100}, pity={})
        for _ in range(5):
            drawer.next()
        lines = M.format_draw_trace(drawer.last_trace).splitlines()
        self.assertIn("1★100.0(100)%", lines[2])
        self.assertIn("出货计数：6★0 / 5★0 / 4★0 / 3★0 / 2★0 / 1★5", lines[1])


class ArknightsDefaultTest(unittest.TestCase):
    """默认配置：6★ 约 2% / 5★ 约 8% / 4★ 约 50% / 3★ 约 40%，1★/2★ 少量但可抽。"""

    def test_default_weights(self):
        self.assertEqual(
            {r: M.DEFAULT_RARITY_WEIGHTS[f"weight_{r}"] for r in M.RARITY_ORDER},
            {6: 2, 5: 8, 4: 50, 3: 40, 2: 1, 1: 0.1},
        )
        # 1.6 起最低两档必须可抽
        self.assertGreater(M.DEFAULT_RARITY_WEIGHTS["weight_2"], 0)
        self.assertGreater(M.DEFAULT_RARITY_WEIGHTS["weight_1"], 0)
        self.assertEqual(M.DEFAULT_PITY["pity_6"], 10)
        self.assertEqual(M.DEFAULT_SUBMIT_RARITY, 3)  # 新投稿落在最低可抽档

    def test_default_rates(self):
        random.seed(20261018)
        d = make_library({r: 2 for r in M.RARITY_ORDER})
        weights = {r: M.DEFAULT_RARITY_WEIGHTS[f"weight_{r}"] for r in M.RARITY_ORDER}
        drawer = M.QuoteDrawer(d, window=0, weights=weights, pity={})
        names = draw_many(drawer, 20000)
        got = {r: sum(1 for n in names if rarity_of(n) == r) / len(names) for r in M.RARITY_ORDER}
        self.assertAlmostEqual(got[6], 0.02, delta=0.01)
        self.assertAlmostEqual(got[5], 0.08, delta=0.015)
        self.assertAlmostEqual(got[4], 0.50, delta=0.02)
        self.assertAlmostEqual(got[3], 0.40, delta=0.02)
        self.assertGreater(got[2], 0.005)   # 约 1%
        self.assertLess(got[2], 0.015)
        self.assertGreater(got[1], 0)       # 约 0.1%
        self.assertLess(got[1], 0.004)

    def test_star_labels_are_six_tiers(self):
        self.assertEqual(
            [M.RARITY_TEXT[r] for r in M.RARITY_ORDER],
            ["★★★★★★", "★★★★★", "★★★★", "★★★", "★★", "★"],
        )
        self.assertEqual(M.parse_quote_rarity("1.a.jpg")[0], 1)
        self.assertEqual(M.parse_quote_rarity("6.a.jpg")[0], 6)
        self.assertEqual(M.parse_quote_rarity("7.a.jpg")[0], 1)   # 越界按 1★
        self.assertEqual(M.parse_quote_rarity("a.jpg")[0], 1)


class DrawStatsTest(unittest.TestCase):
    """累计出货计数：与保底无关，用来核对概率。"""

    def test_stats_persist_and_match_draws(self):
        random.seed(20261019)
        d = make_library({6: 1, 4: 2, 3: 3})
        weights = {6: 2, 4: 50, 3: 40}
        drawer = M.QuoteDrawer(d, window=0, weights=weights, pity={})
        for _ in range(200):
            drawer.next()
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        stats = state["stats"]
        self.assertEqual(sum(int(v) for v in stats.values()), 200)
        self.assertEqual(drawer.last_trace["total_draws"], 200)
        self.assertEqual(drawer.last_trace["stats"][6], int(stats["6"]))
        line = M.format_draw_trace(drawer.last_trace).splitlines()[1]
        self.assertTrue(line.startswith("出货计数："))
        pairs = re.findall(r"\d★(\d+)", line)
        self.assertEqual(len(pairs), 6)
        self.assertEqual(sum(int(value) for value in pairs), 200)

    def test_stats_are_independent_of_pity(self):
        """保底只改变抽到什么，计数按实际抽到的星级累加。"""
        random.seed(20261020)
        d = make_library({6: 1, 3: 20})
        drawer = M.QuoteDrawer(d, window=0, weights={6: 0.0, 3: 100.0}, pity={6: 2})
        for _ in range(10):
            drawer.next()
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        self.assertEqual(int(state["stats"]["6"]), 5)   # 每 2 抽保底一次 6★
        self.assertEqual(int(state["stats"]["3"]), 5)
        self.assertEqual(drawer.last_trace["pity"]["triggered"], 6)


if __name__ == "__main__":
    unittest.main(verbosity=2)
