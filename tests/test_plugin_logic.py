"""插件功能集成测试：用 stub_astrbot 桩导入 main.py 后调用真实 handler。

覆盖：按群调试模式、群设置指令、抽卡指令、戳一戳、黑名单、
投稿→改名→改稀有→删除/撤回全流程、AI Tools 权限、Web API。

运行：
    python3 tests/test_plugin_logic.py
    # 或
    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import unittest
from pathlib import Path

from stub_astrbot import (
    AiocqhttpMessageEvent,
    AstrMessageEvent,
    Context,
    Image,
    Reply,
    drive,
    install,
    run,
)

ENV = install()  # 必须在 import main 之前

import main as M  # noqa: E402

ADMIN = "10001"
MEMBER = "20002"
GROUP = "123456"
OTHER_GROUP = "654321"


def event(user=MEMBER, group=GROUP, message=None, raw=None, text="", responses=None):
    return AstrMessageEvent(
        group_id=group,
        user_id=user,
        message=message or [],
        raw_message=raw,
        message_str=text,
        responses=responses,
    )


def poke_event(user=MEMBER, group=GROUP):
    raw = {
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "poke",
        "self_id": "99999",
        "user_id": user,
        "target_id": "99999",
        "group_id": group,
    }
    return AstrMessageEvent(
        group_id=group, user_id=user, raw_message=raw, bot_id="99999"
    )


def first_text(results):
    for kind, payload in results:
        if kind == "plain":
            return payload
    return ""


class PluginTestBase(unittest.TestCase):
    def setUp(self):
        shutil.rmtree(ENV.data_root, ignore_errors=True)
        ENV.logger.records.clear()
        ENV.request.reset()
        self.ctx = Context(admins=[ADMIN])
        self.plugin = M.QuotePlugin(self.ctx, {})

    # 便捷断言
    def assertOk(self, result):
        self.assertTrue(result[0], f"应当成功，实际返回：{result}")

    def assertFail(self, result, keyword=""):
        self.assertFalse(result[0], f"应当失败，实际返回：{result}")
        if keyword:
            self.assertIn(keyword, result[1])


class DebugModeTest(PluginTestBase):
    def test_debug_mode_is_per_group(self):
        self.assertOk(self.plugin._apply_group_settings(GROUP, debug_mode=2))
        self.assertEqual(self.plugin._debug_level(GROUP), 2)
        self.assertEqual(self.plugin._debug_level(OTHER_GROUP), 0)
        settings = ENV.read_json(ENV.group_dir(GROUP) / "settings.json")
        self.assertEqual(settings["debug_mode"], 2)
        self.assertFalse((ENV.group_dir(OTHER_GROUP) / "settings.json").exists())

    def test_invalid_debug_mode_is_rejected(self):
        self.plugin._apply_group_settings(GROUP, debug_mode=2)
        self.assertFail(self.plugin._apply_group_settings(GROUP, debug_mode=5))
        self.assertFail(self.plugin._apply_group_settings(GROUP, debug_mode="abc"))
        self.assertEqual(self.plugin._debug_level(GROUP), 2)  # 未被写坏

    def test_global_settings_reject_debug_mode(self):
        ok, text, _ = self.plugin._apply_global_settings({"debug_mode": 1})
        self.assertFalse(ok)
        self.assertIn("按群设置", text)
        self.assertNotIn("debug_mode", self.plugin._load_global_settings())

    def test_draw_output_respects_group_level(self):
        for group in (GROUP, OTHER_GROUP):
            ENV.put_image(group, "3.demo.jpg")
        self.plugin._apply_group_settings(GROUP, debug_mode=2)

        path, note = self.plugin._draw_quote(GROUP, "指令 /语录", MEMBER)
        self.assertTrue(path)
        self.assertIn("出货计数", note)
        self.assertIn("[语录调试]", note)
        self.assertIn("(", note)
        self.assertIn(f"群{GROUP}", ENV.logger.text())

        ENV.logger.records.clear()
        path2, note2 = self.plugin._draw_quote(OTHER_GROUP, "指令 /语录", MEMBER)
        self.assertTrue(path2)
        self.assertEqual(note2, "")  # 其它群不受影响
        self.assertNotIn("语录调试", ENV.logger.text())

    def test_debug_note_stays_before_image(self):
        ENV.put_image(GROUP, "3.demo.jpg")
        target = self.plugin._find_quote_file(GROUP, "demo")
        chain = self.plugin._with_debug_note(M.quote_chain(str(target)), "DBG-HEAD")
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[0].text.splitlines()[0], "DBG-HEAD")
        self.assertIn("demo.jpg", chain[0].text)  # 原卡片文字仍在
        self.assertIsInstance(chain[1], Image)  # 图片仍在最后
        # 空附注不改变消息链
        self.assertEqual(self.plugin._with_debug_note(chain, ""), chain)

    def test_command_sets_current_group(self):
        results = drive(self.plugin.set_debug_mode(event(user=ADMIN), "1"))
        self.assertIn("本群语录调试模式已设置为：1", first_text(results))
        self.assertEqual(self.plugin._debug_level(GROUP), 1)

        results = drive(self.plugin.set_debug_mode(event(user=ADMIN), "9"))
        self.assertIn("调试模式只能是 0、1、2", first_text(results))
        self.assertEqual(self.plugin._debug_level(GROUP), 1)

        results = drive(self.plugin.set_debug_mode(event(user=ADMIN), ""))
        self.assertIn("用法：/语录调试", first_text(results))

    def test_status_lists_every_rarity(self):
        self.plugin._apply_group_settings(GROUP, debug_mode=2)
        ENV.put_image(GROUP, "6.甲.jpg")
        ENV.put_image(GROUP, "3.乙.jpg")
        text = first_text(drive(self.plugin.quote_status(event())))
        self.assertIn("总数：2 张", text)
        for rarity in M.RARITY_ORDER:
            self.assertIn(f"{rarity}★ ", text)
        self.assertIn("6★ 1 张", text)
        self.assertIn("3★ 1 张", text)
        self.assertIn("1★ 0 张", text)   # 空档位也要列出
        self.assertIn("调试模式：2", text)
        self.assertIn("调试模式：2", self.plugin._quote_stats_text(GROUP))


class CommandTest(PluginTestBase):
    def test_group_settings_commands(self):
        self.assertIn("投稿权限已设置为：全体成员",
                      first_text(drive(self.plugin.set_submit_mode(event(user=ADMIN), 2))))
        self.assertEqual(self.plugin._load_settings(GROUP)["mode"], 2)

        self.assertIn("戳戳冷却已设置为：5 秒",
                      first_text(drive(self.plugin.set_poke_cooldown(event(user=ADMIN), 5))))
        self.assertEqual(self.plugin._load_settings(GROUP)["cooldown"], 5)

        self.assertIn("模式只能是 0、1、2",
                      first_text(drive(self.plugin.set_submit_mode(event(user=ADMIN), 9))))

    def test_random_quote_returns_chain(self):
        ENV.put_image(GROUP, "6.only.jpg")
        results = drive(self.plugin.random_quote(event()))
        self.assertEqual(results[0][0], "chain")
        chain = results[0][1]
        self.assertIsInstance(chain[0], M.Comp.Plain)
        self.assertIsInstance(chain[-1], M.Comp.Image)
        self.assertIn("★★★★★★", chain[0].text)

    def test_random_quote_empty_library(self):
        results = drive(self.plugin.random_quote(event()))
        self.assertIn("本群还没有群友语录", first_text(results))

    def test_blacklist_blocks_draw(self):
        ENV.put_image(GROUP, "1.a.jpg")
        self.plugin._update_blacklist("block", MEMBER)
        self.assertTrue(self.plugin._is_blocked_user(MEMBER))
        results = drive(self.plugin.random_quote(event(user=MEMBER)))
        self.assertIn("已被语录系统拉黑", first_text(results))
        # 解封后恢复
        self.plugin._update_blacklist("unblock", MEMBER)
        results = drive(self.plugin.random_quote(event(user=MEMBER)))
        self.assertEqual(results[0][0], "chain")

    def test_block_command_resolves_at_or_uid(self):
        results = drive(self.plugin.block_quote_user(event(user=ADMIN, message=[
            M.Comp.At(qq=MEMBER)
        ]), ""))
        self.assertIn("已全局拉黑用户", first_text(results))
        self.assertTrue(self.plugin._is_blocked_user(MEMBER))
        results = drive(self.plugin.unblock_quote_user(event(user=ADMIN), MEMBER))
        self.assertIn("已解除用户", first_text(results))


class PokeTest(PluginTestBase):
    def test_poke_draws_then_cooldown_blocks(self):
        ENV.put_image(GROUP, "3.demo.jpg")
        self.plugin._apply_global_settings({"poke_quote_probability": 1.0})
        self.plugin._apply_group_settings(GROUP, cooldown=60)
        self.plugin._drawers.clear()

        first = drive(self.plugin.on_poke(poke_event()))
        self.assertEqual(first[0][0], "chain")

        second = drive(self.plugin.on_poke(poke_event()))
        self.assertEqual(second, [])  # 冷却中，静默

    def test_blocked_user_poke_is_ignored_without_consuming_cooldown(self):
        ENV.put_image(GROUP, "3.demo.jpg")
        self.plugin._apply_global_settings({"poke_quote_probability": 1.0})
        self.plugin._update_blacklist("block", MEMBER)

        self.assertEqual(drive(self.plugin.on_poke(poke_event(user=MEMBER))), [])
        self.assertEqual(self.plugin._load_settings(GROUP)["last_poke"], 0)

    def test_poke_only_reacts_to_bot_target(self):
        ENV.put_image(GROUP, "3.demo.jpg")
        self.plugin._apply_global_settings({"poke_quote_probability": 1.0})
        raw = {
            "post_type": "notice", "notice_type": "notify", "sub_type": "poke",
            "self_id": "99999", "user_id": MEMBER, "target_id": MEMBER,
            "group_id": GROUP,
        }
        ev = AstrMessageEvent(group_id=GROUP, user_id=MEMBER, raw_message=raw, bot_id="99999")
        self.assertEqual(drive(self.plugin.on_poke(ev)), [])

    def test_probability_zero_always_sends_fun_text(self):
        ENV.put_image(GROUP, "3.demo.jpg")
        self.plugin._apply_global_settings({"poke_quote_probability": 0.0})
        results = drive(self.plugin.on_poke(poke_event()))
        self.assertEqual(results[0][0], "chain")
        chain = results[0][1]
        self.assertIsInstance(chain[0], M.Comp.At)
        self.assertIn(chain[1].text, M.POKE_TEXTS)


class SubmissionTest(PluginTestBase):
    def _submit(self, title, user=MEMBER, group=GROUP):
        src = ENV.workdir / "src.png"
        src.write_bytes(b"png-bytes")
        ev = AiocqhttpMessageEvent(
            group_id=group,
            user_id=user,
            message=[Reply(id="55"), Image(path=str(src))],
            responses={
                "get_msg": {
                    "message": [
                        {"type": "image", "data": {"file": "a.png", "url": "http://x/a.png"}}
                    ]
                }
            },
        )
        return run(self.plugin._submit_replied_quote(ev, group, user, title))

    def test_submit_requires_mode(self):
        self.assertFail(self._submit("标题"), "投稿系统未开启")
        self.plugin._apply_group_settings(GROUP, mode=2)
        ok, text = self._submit("标题")
        self.assertTrue(ok, text)
        self.assertTrue((ENV.group_dir(GROUP) / "3.标题.png").exists())
        record = self.plugin._find_submission_record(GROUP, "3.标题.png")
        self.assertEqual(record["user_id"], MEMBER)

    def test_duplicate_title_rejected(self):
        self.plugin._apply_group_settings(GROUP, mode=2)
        self.assertTrue(self._submit("重名")[0])
        ok, text = self._submit("重名")
        self.assertFalse(ok)
        self.assertIn("已存在同名语录", text)

    def test_rename_rarity_delete_keep_log_in_sync(self):
        self.plugin._apply_group_settings(GROUP, mode=2)
        self.assertTrue(self._submit("旧标题")[0])
        target = ENV.group_dir(GROUP) / "3.旧标题.png"

        ok, _ = self.plugin._rename_quote_file(GROUP, target, "新标题")
        self.assertTrue(ok)
        renamed = ENV.group_dir(GROUP) / "3.新标题.png"
        self.assertTrue(renamed.exists())
        self.assertIsNone(self.plugin._find_submission_record(GROUP, "3.旧标题.png"))
        self.assertIsNotNone(self.plugin._find_submission_record(GROUP, "3.新标题.png"))

        ok, _ = self.plugin._set_quote_rarity_file(GROUP, renamed, 5)
        self.assertTrue(ok)
        rare = ENV.group_dir(GROUP) / "5.新标题.png"
        self.assertTrue(rare.exists())
        self.assertIsNotNone(self.plugin._find_submission_record(GROUP, "5.新标题.png"))

        ok, _ = self.plugin._delete_quote_file(GROUP, rare)
        self.assertTrue(ok)
        self.assertFalse(rare.exists())
        self.assertIsNone(self.plugin._find_submission_record(GROUP, "5.新标题.png"))
        self.assertEqual(self.plugin._read_submit_log(), [])

    def test_revoke_only_owner(self):
        self.plugin._apply_group_settings(GROUP, mode=2)
        self.assertTrue(self._submit("我的投稿")[0])
        target = ENV.group_dir(GROUP) / "3.我的投稿.png"

        ok, text = self.plugin._revoke_quote_file(GROUP, ADMIN, target)
        self.assertFalse(ok)
        self.assertIn("只能撤回自己投的语录", text)
        self.assertTrue(target.exists())

        ok, text = self.plugin._revoke_quote_file(GROUP, MEMBER, target)
        self.assertTrue(ok, text)
        self.assertFalse(target.exists())

    def test_reply_based_delete_resolves_card(self):
        ENV.put_image(GROUP, "1.卡片.png")
        ev = event(user=ADMIN, message=[Reply(id="77")], responses={
            "get_msg": {"message": [
                {"type": "text", "data": {"text": "您抽到了新卡\n卡片.png"}}
            ]}
        })
        results = drive(self.plugin.delete_quote(ev, ""))
        self.assertIn("已删除语录", first_text(results))
        self.assertFalse((ENV.group_dir(GROUP) / "1.卡片.png").exists())

    def test_find_quote_file_matches_several_forms(self):
        self.plugin._apply_group_settings(GROUP, mode=2)
        self.plugin._ensure_group_dir(GROUP)
        (ENV.group_dir(GROUP) / "3.杰克的比喻.jpg").write_bytes(b"x")
        for query in ("3.杰克的比喻.jpg", "杰克的比喻.jpg", "杰克的比喻"):
            found = self.plugin._find_quote_file(GROUP, query)
            self.assertIsNotNone(found, f"应当能按 {query!r} 找到")
            self.assertEqual(found.name, "3.杰克的比喻.jpg")
        self.assertIsNone(self.plugin._find_quote_file(GROUP, "不存在"))
        self.assertEqual(M.parse_quote_rarity("3.杰克的比喻.jpg"), (3, "杰克的比喻.jpg"))


class AiToolTest(PluginTestBase):
    def setUp(self):
        super().setUp()
        self.plugin._apply_group_settings(GROUP, mode=2, debug_mode=1)
        ENV.put_image(GROUP, "3.甲.jpg")
        ENV.put_image(GROUP, "6.乙.jpg")
        self.admin_ev = event(user=ADMIN)
        self.member_ev = event(user=MEMBER)

    def test_list_and_view(self):
        text = run(self.plugin.ai_quote_list(self.admin_ev, 0, 1, 10))
        self.assertIn("语录列表：共 2 张", text)
        self.assertIn("[★★★] 甲.jpg", text)
        self.assertNotIn(str(ENV.data_root), text)  # 不泄露本地路径

        text = run(self.plugin.ai_quote_list(self.admin_ev, 6, 1, 10))
        self.assertIn("筛选 6 星", text)

        text = run(self.plugin.ai_quote_view(self.admin_ev, "乙"))
        self.assertIn("★★★★★★", text)

    def test_status(self):
        text = run(self.plugin.ai_quote_status(self.member_ev))
        self.assertIn("总数：2 张", text)
        self.assertIn("6★ 1 张", text)
        self.assertIn("1★ 0 张", text)
        self.assertIn("调试模式：1", text)

    def test_writes_require_admin(self):
        self.assertEqual(run(self.plugin.ai_quote_delete(self.member_ev, "甲")),
                         "权限不足，仅 bot 管理员可删除语录。")
        self.assertIn("权限不足", run(self.plugin.ai_quote_rename(self.member_ev, "甲", "丙")))
        self.assertIn("权限不足", run(self.plugin.ai_quote_configure_group(self.member_ev, 2, None, None)))
        self.assertIn("权限不足", run(self.plugin.ai_quote_manage_blacklist(self.member_ev, "block", MEMBER)))

        self.assertIn("已删除", run(self.plugin.ai_quote_delete(self.admin_ev, "甲")))
        self.assertFalse((ENV.group_dir(GROUP) / "3.甲.jpg").exists())

    def test_configure_group_debug_mode(self):
        self.assertIn("群设置已保存",
                      run(self.plugin.ai_quote_configure_group(self.admin_ev, None, None, 2)))
        self.assertEqual(self.plugin._debug_level(GROUP), 2)
        self.assertIn("请至少提供",
                      run(self.plugin.ai_quote_configure_group(self.admin_ev, None, None, None)))

    def test_draw_sends_card_and_returns_text(self):
        ev = event(user=ADMIN)
        text = run(self.plugin.ai_quote_draw(ev))
        self.assertEqual(text, "已完成抽卡并发送到群内。")
        self.assertEqual(len(ev.sent), 1)
        self.assertEqual(ev.sent[0][0], "chain")
        # 该群档位为 1（仅日志）：卡片上不应有附注，但日志里应有
        self.assertNotIn("语录调试", ev.sent[0][1][0].text)
        self.assertIn("语录调试", ENV.logger.text())

    def test_draw_with_debug_two_attaches_note(self):
        self.plugin._apply_group_settings(GROUP, debug_mode=2)
        ev = event(user=ADMIN)
        run(self.plugin.ai_quote_draw(ev))
        self.assertIn("出货计数", ev.sent[0][1][0].text)

    def test_submit_tool_permission(self):
        text = run(self.plugin.ai_quote_submit(self.member_ev, "标题"))
        self.assertIn("请回复一条含图消息", text)

    def test_blacklisted_user_cannot_use_tools(self):
        self.plugin._update_blacklist("block", MEMBER)
        self.assertIn("拉黑", run(self.plugin.ai_quote_list(self.member_ev, 0, 1, 10)))


class WebApiTest(PluginTestBase):
    def test_routes_registered(self):
        self.assertEqual(len(self.ctx.apis), 12)
        self.assertIn(("/quote_collocter/group-debug", ["GET"]), self.ctx.apis)
        self.assertIn(("/quote_collocter/group-debug", ["POST"]), self.ctx.apis)

    def test_group_debug_get_and_post(self):
        self.plugin._apply_group_settings(GROUP, debug_mode=2)
        ENV.request.reset({"group_id": GROUP})
        data = run(self.plugin.web_get_group_debug())["ok"]
        self.assertEqual(data["debug_mode"], 2)
        self.assertEqual(data["group_id"], GROUP)

        ENV.request.reset({}, {"group_id": OTHER_GROUP, "debug_mode": 1})
        data = run(self.plugin.web_set_group_debug())["ok"]
        self.assertEqual(data["debug_mode"], 1)
        self.assertEqual(self.plugin._debug_level(OTHER_GROUP), 1)

        ENV.request.reset({"group_id": "abc"})
        self.assertIn("err", run(self.plugin.web_get_group_debug()))

        ENV.request.reset({}, {"group_id": GROUP, "debug_mode": 9})
        self.assertIn("err", run(self.plugin.web_set_group_debug()))

    def test_settings_get_and_save(self):
        ENV.request.reset()
        data = run(self.plugin.web_get_settings())["ok"]
        self.assertNotIn("debug_mode", data)  # 已改为按群

        ENV.request.reset({}, {"recent_window": 0, "poke_quote_probability": 0.5})
        saved = run(self.plugin.web_save_settings())["ok"]
        self.assertEqual(saved["recent_window"], 0)
        self.assertEqual(self.plugin._load_global_settings()["recent_window"], 0)

        ENV.request.reset({}, {"recent_window": -1})
        self.assertIn("err", run(self.plugin.web_save_settings()))

    def test_submit_log_filter_and_limit(self):
        # 造 3 条日志：GROUP 两条、OTHER_GROUP 一条
        for i in range(2):
            self.plugin._log_submission(GROUP, MEMBER, f"1.x{i}.jpg", 100 + i)
        self.plugin._log_submission(OTHER_GROUP, ADMIN, "1.y.jpg", 300)

        ENV.request.reset({"group_id": GROUP})
        data = run(self.plugin.web_submit_log())["ok"]
        self.assertEqual(data["total"], 2)
        self.assertTrue(all(r["group_id"] == GROUP for r in data["items"]))
        # 倒序
        self.assertEqual(data["items"][0]["filename"], "1.x1.jpg")

        ENV.request.reset({"limit": 1})
        data = run(self.plugin.web_submit_log())["ok"]
        self.assertEqual(data["total"], 1)


class MigrationTest(PluginTestBase):
    """1..5 档旧数据 → 1..6 星（1.1 起）"""

    def _seed_legacy(self):
        root = ENV.data_root
        marker = root / ".rarity_scale"
        if marker.exists():
            marker.unlink()
        group = ENV.group_dir("100")
        group.mkdir(parents=True, exist_ok=True)
        for name in ("1.a.jpg", "3.c.jpg", "5.b.jpg"):
            (group / name).write_bytes(b"x")
        records = [
            {"ts": 1, "time": "t1", "group_id": "100", "user_id": "u1", "filename": "1.a.jpg"},
            {"ts": 2, "time": "t2", "group_id": "100", "user_id": "u2", "filename": "5.b.jpg"},
        ]
        (root / "submit.log").write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
            encoding="utf-8",
        )
        (group / "order.json").write_text(
            json.dumps({"since": {"1": 5, "5": 2}, "recent": ["1.a.jpg"]}),
            encoding="utf-8",
        )
        (root / "settings.json").write_text(
            json.dumps(
                {
                    "rarity_weights": {"weight_1": 2, "weight_5": 3},
                    "pity_config": {"pity_5": 10},
                }
            ),
            encoding="utf-8",
        )
        return group

    def test_legacy_scale_is_migrated(self):
        group = self._seed_legacy()
        plugin = M.QuotePlugin(self.ctx, {})  # __init__ 触发迁移

        names = sorted(p.name for p in group.iterdir() if p.suffix == ".jpg")
        self.assertEqual(names, ["2.a.jpg", "4.c.jpg", "6.b.jpg"])

        log = [r["filename"] for r in plugin._read_submit_log()]
        self.assertEqual(log, ["2.a.jpg", "6.b.jpg"])

        order = ENV.read_json(group / "order.json")
        self.assertEqual(order["since"], {"1": 0, "2": 5, "3": 0, "4": 0, "5": 0, "6": 2})
        self.assertEqual(order["recent"], ["2.a.jpg"])

        settings = plugin._load_global_settings()
        self.assertEqual(
            {r: settings["rarity_weights"][f"weight_{r}"] for r in M.RARITY_ORDER},
            {6: 2, 5: 8, 4: 50, 3: 40, 2: 1, 1: 0.1},
        )
        self.assertEqual(settings["pity_config"]["pity_6"], 10)
        self.assertEqual(ENV.read_json(ENV.data_root / ".rarity_scale")["scale"], "6-star")

    def test_migration_is_idempotent(self):
        group = self._seed_legacy()
        M.QuotePlugin(self.ctx, {})
        before = sorted(p.name for p in group.iterdir() if p.suffix == ".jpg")
        M.QuotePlugin(self.ctx, {})  # 第二次启动不应再改名
        after = sorted(p.name for p in group.iterdir() if p.suffix == ".jpg")
        self.assertEqual(before, after)
        self.assertIn("6.b.jpg", after)


class ManageApiTest(PluginTestBase):
    """管理页后端：列表 / 改星级 / 删图 / 缩略图"""

    def setUp(self):
        super().setUp()
        for name in ("3.a.jpg", "4.b.jpg", "6.c.jpg"):
            ENV.put_image(GROUP, name)
        self.plugin._apply_group_settings(GROUP, mode=2)
        self.plugin._log_submission(GROUP, MEMBER, "3.a.jpg", 1)

    def test_list_filter_and_paging(self):
        ENV.request.reset({"group_id": GROUP})
        data = run(self.plugin.web_quotes())["ok"]
        self.assertEqual(data["total"], 3)
        item = next(i for i in data["items"] if i["name"] == "3.a.jpg")
        self.assertEqual(item["stars"], "★★★")
        self.assertEqual(item["submitter"], MEMBER)
        self.assertIn("quotes/image", item["image_url"])

        ENV.request.reset({"group_id": GROUP, "rarity": 6})
        self.assertEqual(run(self.plugin.web_quotes())["ok"]["total"], 1)

        ENV.request.reset({"group_id": GROUP, "query": "b"})
        self.assertEqual(run(self.plugin.web_quotes())["ok"]["total"], 1)

        ENV.request.reset({"group_id": GROUP, "page_size": 2, "page": 2})
        data = run(self.plugin.web_quotes())["ok"]
        self.assertEqual((data["page"], data["pages"], len(data["items"])), (2, 2, 1))

    def test_set_rarity_single_and_bulk(self):
        ENV.request.reset({}, {"group_id": GROUP, "names": ["3.a.jpg"], "rarity": 6})
        res = run(self.plugin.web_set_quote_rarity())["ok"]
        self.assertEqual(res["changed"], 1)
        self.assertTrue((ENV.group_dir(GROUP) / "6.a.jpg").exists())

        ENV.request.reset(
            {},
            {"group_id": GROUP, "rarity": 4, "filter_rarity": 0, "filter_query": ""},
        )
        res = run(self.plugin.web_set_quote_rarity())["ok"]
        names = sorted(
            p.name for p in ENV.group_dir(GROUP).iterdir() if p.suffix == ".jpg"
        )
        self.assertEqual(names, ["4.a.jpg", "4.b.jpg", "4.c.jpg"])
        self.assertEqual(res["changed"] + res["skipped"], 3)

    def test_image_endpoint_and_traversal(self):
        ENV.request.reset({"group_id": GROUP, "name": "4.b.jpg"})
        res = run(self.plugin.web_quote_image())
        self.assertTrue(res["file"].endswith("4.b.jpg"))
        self.assertEqual(res["content_type"], "image/jpeg")

        ENV.request.reset({"group_id": GROUP, "name": "../../settings.json"})
        self.assertIn("err", run(self.plugin.web_quote_image()))

    def test_delete_quote(self):
        ENV.request.reset({}, {"group_id": GROUP, "names": ["4.b.jpg"]})
        res = run(self.plugin.web_delete_quotes())["ok"]
        self.assertEqual(res["deleted"], 1)
        self.assertFalse((ENV.group_dir(GROUP) / "4.b.jpg").exists())
        self.assertIsNone(self.plugin._find_submission_record(GROUP, "4.b.jpg"))

    def test_invalid_params(self):
        ENV.request.reset({"group_id": "abc"})
        self.assertIn("err", run(self.plugin.web_quotes()))
        ENV.request.reset({}, {"group_id": GROUP, "rarity": 9})
        self.assertIn("err", run(self.plugin.web_set_quote_rarity()))
        ENV.request.reset({}, {"group_id": GROUP, "names": []})
        self.assertIn("err", run(self.plugin.web_delete_quotes()))


class ManageSortRenameTest(PluginTestBase):
    """管理页：排序、改名、清零出货计数"""

    def setUp(self):
        super().setUp()
        for name in ("3.b.jpg", "4.a.jpg", "6.c.jpg"):
            ENV.put_image(GROUP, name)
        self.plugin._log_submission(GROUP, "20002", "4.a.jpg", 100)
        self.plugin._log_submission(GROUP, "10001", "3.b.jpg", 200)

    def test_sort_by_rarity_and_display(self):
        ENV.request.reset({"group_id": GROUP, "sort": "rarity", "order": "desc"})
        data = run(self.plugin.web_quotes())["ok"]
        self.assertEqual([i["rarity"] for i in data["items"]], [6, 4, 3])
        self.assertEqual(data["sort"], "rarity")

        ENV.request.reset({"group_id": GROUP, "sort": "display", "order": "asc"})
        data = run(self.plugin.web_quotes())["ok"]
        self.assertEqual([i["display"] for i in data["items"]], ["a.jpg", "b.jpg", "c.jpg"])

        ENV.request.reset({"group_id": GROUP, "sort": "size", "order": "asc"})
        self.assertEqual(run(self.plugin.web_quotes())["ok"]["sort"], "size")
        # 未知排序键回退 name
        ENV.request.reset({"group_id": GROUP, "sort": "bogus"})
        self.assertEqual(run(self.plugin.web_quotes())["ok"]["sort"], "name")

    def test_rename_quote(self):
        ENV.request.reset({}, {"group_id": GROUP, "name": "4.a.jpg", "new_title": "新标题"})
        res = run(self.plugin.web_rename_quote())["ok"]
        self.assertTrue((ENV.group_dir(GROUP) / "4.新标题.jpg").exists())
        self.assertFalse((ENV.group_dir(GROUP) / "4.a.jpg").exists())
        self.assertTrue(res["message"])
        self.assertEqual(res["rows"]["total"], 3)

        # 重名被拒
        ENV.request.reset({}, {"group_id": GROUP, "name": "3.b.jpg", "new_title": "新标题"})
        self.assertIn("err", run(self.plugin.web_rename_quote()))

        ENV.request.reset({}, {"group_id": GROUP, "name": "3.b.jpg", "new_title": ""})
        self.assertIn("err", run(self.plugin.web_rename_quote()))

    def test_reset_draw_stats(self):
        state = ENV.group_dir(GROUP) / "order.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(
            json.dumps({"since": {"6": 3}, "recent": [], "stats": {"6": 5, "3": 7}}),
            encoding="utf-8",
        )
        ENV.request.reset({}, {"group_id": GROUP})
        res = run(self.plugin.web_reset_draw_stats())["ok"]
        self.assertEqual(res["total_draws"], 0)
        saved = ENV.read_json(state)
        self.assertEqual(saved["stats"], {str(r): 0 for r in M.RARITY_ORDER})
        self.assertEqual(saved["since"], {"6": 3})  # 保底计数不受影响


class QuoteImageApiTest(PluginTestBase):
    """管理页图片：缩略图 data URL（Pillow）与完整图接口。"""

    def setUp(self):
        super().setUp()
        ENV.put_image(GROUP, "3.solid.jpg", content=b"not-a-real-image")

    def test_thumb_field_and_full_image(self):
        ENV.request.reset({"group_id": GROUP})
        item = run(self.plugin.web_quotes())["ok"]["items"][0]
        self.assertIn("thumb", item)
        # 伪造的图片字节：有 Pillow 时缩略图为 None，无 Pillow 时同样为 None
        self.assertIn(item["thumb"], (None,))

        ENV.request.reset({"group_id": GROUP, "name": "3.solid.jpg"})
        res = run(self.plugin.web_quote_full())["ok"]
        self.assertTrue(res["data_url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(res["name"], "3.solid.jpg")

        ENV.request.reset({"group_id": GROUP, "name": "不存在.jpg"})
        self.assertIn("err", run(self.plugin.web_quote_full()))


class WeightsUpgradeTest(PluginTestBase):
    """1.6：旧默认权重（2★/1★ 为 0）自动升级，自定义权重不动。"""

    def _seed(self, weights):
        path = ENV.data_root / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"rarity_weights": {f"weight_{r}": weights[r] for r in M.RARITY_ORDER}}),
            encoding="utf-8",
        )
        marker = ENV.data_root / ".weights_rev"
        if marker.exists():
            marker.unlink()

    def test_legacy_default_is_upgraded(self):
        self._seed({6: 2, 5: 8, 4: 50, 3: 40, 2: 0, 1: 0})
        plugin = M.QuotePlugin(self.ctx, {})
        saved = plugin._load_rarity_weights()
        self.assertGreater(saved[2], 0)
        self.assertGreater(saved[1], 0)
        self.assertEqual((saved[6], saved[5], saved[4], saved[3]), (2, 8, 50, 40))

    def test_custom_weights_are_untouched(self):
        custom = {6: 19, 5: 110, 4: 370, 3: 490, 2: 10, 1: 1}
        self._seed(custom)
        plugin = M.QuotePlugin(self.ctx, {})
        self.assertEqual(plugin._load_rarity_weights(), custom)

    def test_upgrade_runs_once(self):
        self._seed({6: 2, 5: 8, 4: 50, 3: 40, 2: 0, 1: 0})
        M.QuotePlugin(self.ctx, {})
        # 标记已写入；此时手动改回旧值也不应再被自动改
        path = ENV.data_root / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["rarity_weights"] = {f"weight_{r}": 0 for r in M.RARITY_ORDER}
        path.write_text(json.dumps(data), encoding="utf-8")
        plugin = M.QuotePlugin(self.ctx, {})
        self.assertEqual(plugin._load_rarity_weights()[1], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
