"""抽卡概率模型测试（纯逻辑，不需要 AstrBot）。

运行：
    python3 tests/test_draw_probability.py
    # 或
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import random
import tempfile
import unittest
from pathlib import Path

from stub_astrbot import install

install()  # 必须在 import main 之前

import main as M  # noqa: E402

WEIGHTS = {5: 3.0, 4: 5.0, 3: 8.0, 2: 4.0, 1: 2.0}
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
        d = make_library({5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL,
                                   msg=f"{rarity}★ 出率 {got:.3f} != {expected:.3f}")

    def test_dedup_does_not_distort_rarity_rates(self):
        """去重窗口只影响"选哪张"，不应改变星级出率。"""
        random.seed(12)
        d = make_library({5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL,
                                   msg=f"{rarity}★ 出率 {got:.3f} != {expected:.3f}")

    def test_rates_ignore_image_count_per_rarity(self):
        """同样的权重，图库张数比例不同，出率也应相同（旧版是张数比例）。"""
        random.seed(13)
        d = make_library({5: 1, 4: 1, 3: 10, 2: 1, 1: 20})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={})
        names = draw_many(drawer, 8000)
        for rarity, expected in SHARES.items():
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, expected, delta=TOL)

    def test_zero_weight_disables_rarity(self):
        random.seed(14)
        d = make_library({5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights={**WEIGHTS, 5: 0.0}, pity={})
        names = draw_many(drawer, 3000)
        self.assertEqual(sum(1 for n in names if rarity_of(n) == 5), 0)

    def test_all_zero_weights_fall_back_to_uniform(self):
        random.seed(15)
        d = make_library({5: 2, 4: 2, 3: 2, 2: 2, 1: 2})
        drawer = M.QuoteDrawer(d, window=0, weights={r: 0.0 for r in WEIGHTS}, pity={})
        names = draw_many(drawer, 5000)
        for rarity in WEIGHTS:
            got = sum(1 for n in names if rarity_of(n) == rarity) / len(names)
            self.assertAlmostEqual(got, 0.2, delta=TOL)

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
        d = make_library({5: 1})
        drawer = M.QuoteDrawer(d, window=8, weights={5: 1.0}, pity={})
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
        d = make_library({5: 1, 4: 1, 3: 2, 2: 5, 1: 20})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={5: 10})
        rarities = [rarity_of(n) for n in draw_many(drawer, 4000)]
        pos = [i for i, r in enumerate(rarities) if r == 5]
        self.assertTrue(pos, "4000 抽里应当出现 5★")
        gaps = [b - a for a, b in zip(pos, pos[1:])]
        self.assertLessEqual(max(gaps), 10, f"5★ 最大间隔 {max(gaps)} 超过保底阈值")

    def test_pity_threshold_of_one_hits_every_draw(self):
        random.seed(32)
        d = make_library({5: 1, 1: 5})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={5: 1})
        for _ in range(20):
            drawer.next()
            self.assertEqual(drawer.last_trace["pity"]["triggered"], 5)

    def test_pity_without_target_rarity_keeps_counter_then_fires(self):
        random.seed(33)
        d = make_library({1: 20, 3: 2})  # 没有 5★
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={5: 10})
        for _ in range(15):
            drawer.next()
        trace = drawer.last_trace
        self.assertIsNone(trace["pity"]["triggered"])
        self.assertIn(5, trace["pity"]["skipped"])
        self.assertEqual(trace["pity"]["since_used"][5], 10)  # 计数保留

        (d / "5.new.jpg").write_bytes(b"x")  # 补一张 5★
        path = drawer.next()
        self.assertEqual(Path(path).name, "5.new.jpg")  # 下一抽立即命中
        self.assertEqual(drawer.last_trace["pity"]["triggered"], 5)

    def test_pity_counter_resets_only_up_to_drawn_rarity(self):
        """抽到 N 星只清 <= N 星的计数，更高星级计数继续累积。"""
        random.seed(34)
        d = make_library({5: 1, 1: 50})
        drawer = M.QuoteDrawer(d, window=0, weights={5: 0.0, 1: 1.0}, pity={5: 7})
        # 权重 5★=0（不参与正常抽取），但保底仍会把它抽出来
        for _ in range(6):
            drawer.next()
            self.assertEqual(drawer.last_trace["pity"]["since_after"][5], drawer.last_trace["pity"]["since_used"][5])
        drawer.next()  # 第 7 抽达到阈值
        self.assertEqual(drawer.last_trace["pity"]["since_after"][5], 0)


class StateTest(unittest.TestCase):
    def test_state_file_contents(self):
        random.seed(41)
        d = make_library({1: 3})
        drawer = M.QuoteDrawer(d, window=4, weights=WEIGHTS, pity={5: 10})
        drawer.next()
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        self.assertEqual(set(state), {"since", "recent"})
        self.assertEqual(len(state["recent"]), 1)
        self.assertEqual(state["since"]["5"], 1)

    def test_legacy_order_json_is_compatible(self):
        random.seed(42)
        d = make_library({1: 3})
        legacy = {"order": ["1.q0.jpg", "1.q1.jpg", "1.q2.jpg"], "index": 2,
                  "since": {"5": 4}}
        (d / "order.json").write_text(json.dumps(legacy), encoding="utf-8")
        drawer = M.QuoteDrawer(d, window=4, weights=WEIGHTS, pity={5: 10})
        self.assertTrue(drawer.next())
        state = json.loads((d / "order.json").read_text(encoding="utf-8"))
        self.assertEqual(state["since"]["5"], 5)  # 旧计数被继承并 +1

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
        d = make_library({5: 1, 4: 1, 3: 2, 2: 5, 1: 20})
        drawer = M.QuoteDrawer(d, window=8, weights=WEIGHTS, pity={5: 10})
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
        d = make_library({5: 1, 1: 30})
        drawer = M.QuoteDrawer(d, window=0, weights={5: 0.0, 1: 1.0}, pity={5: 2})
        for _ in range(2):
            drawer.next()
        trace = drawer.last_trace
        self.assertEqual(trace["pity"]["triggered"], 5)
        self.assertEqual(trace["rarity_final"], 5)
        self.assertNotEqual(trace["rarity_by_weight"], 5)  # 权重本来抽不到 5★
        self.assertIn("保底覆盖", M.format_draw_trace(trace, verbose=False))

    def test_verbose_has_extra_lines(self):
        random.seed(53)
        d = make_library({1: 3})
        drawer = M.QuoteDrawer(d, window=0, weights=WEIGHTS, pity={})
        drawer.next()
        verbose = M.format_draw_trace(drawer.last_trace, verbose=True)
        compact = M.format_draw_trace(drawer.last_trace, verbose=False)
        self.assertIn("权重：", verbose)
        self.assertIn("出货后计数：", verbose)
        self.assertNotIn("出货后计数：", compact)
        self.assertEqual(len(compact.splitlines()), 4)
        self.assertIn("保底：", compact)


if __name__ == "__main__":
    unittest.main(verbosity=2)
