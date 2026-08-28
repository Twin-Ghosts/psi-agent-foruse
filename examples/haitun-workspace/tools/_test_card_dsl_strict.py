"""Stricter-than-the-suite tests for the card DSL engine.

比既有三套测试(_test_card_dsl / _test_card_dsl_fixes / _test_card_dsl_spec)更严的
地方,按维度:

1. **真实运行时,零 stub** — 既有测试用 stub 顶替 _todo_card_impl/_runtime_paths,
   本套件把 stub 从 sys.modules 弹出后导入真实实现:todo 卡走真的
   ``_build_card_from_state``/``_build_todo_card``(G03 验收「发卡经模板且字节同构」
   固化成永久回归测试),评价卡走真的 ``_render_review_card``。
2. **结构不变量** — 每一张编译产物必须满足:飞书 2.0 骨架、每个回调 value 带
   action/round/action_id、每个 action 都在 handlers 里(无死键)、JSON 可往返。
3. **轮次生命周期穷尽** - 0..19 每一轮重建都换全新动作名(不撞已消费轮);第 21 次
   重建(round>=20)轮次用尽 → 终态只读卡,不再发任何 action(与 todo 卡 locked 一致)。
4. **组合矩阵** - 4 模板 x 全部 16 种子元素组合 = 64 张合法卡全编译且过不变量;
   12 类非法输入全 fail-closed;合法子集与 XSD 逐一对齐。
5. **跨模块一致性** — 三个模块各自声明的轮次上限(_card_dsl._MAX_ROUNDS /
   _review_card_impl._MAX_ROUNDS / _todo_card_impl._UNDO_ROUNDS)必须相等,否则
   预注册深度与重建轮次错位,卡片会在某个轮次静默变成死键。
6. **对抗性模板填充** — 值里带 XML 元字符 / {note} / {rows} 字面量都不得破坏
   结构或二次展开(单遍替换契约),未填充键 fail-closed。

Run:
    python -m pytest _test_card_dsl_strict.py -q -o addopts=""
  or:
    python _test_card_dsl_strict.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest
import unittest.mock as mock
import xml.etree.ElementTree as ET

# ── 真实运行时:弹出既有测试塞进来的 stub,导入真实实现 ───────────────────────
for _name in ("_todo_card_impl", "_runtime_paths", "_card_dsl", "_review_card_impl"):
    sys.modules.pop(_name, None)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl as dsl  # noqa: E402
import _review_card_impl as review  # noqa: E402
import _todo_card_impl as todo  # noqa: E402

try:
    import xmlschema
except ImportError:
    xmlschema = None

_ACTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*_r\d+$")
_TODO_ACTION_RE = re.compile(r"^todo_(tick|untick)_\d+_r\d+$")


def _walk_callback_values(card: dict):
    """Yield every callback value dict embedded anywhere in the card."""
    stack = [card]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "callback" and isinstance(node.get("value"), dict):
                yield node["value"]
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def _walk_tags(card: dict) -> set[str]:
    tags: set[str] = set()
    stack = [card]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if isinstance(node.get("tag"), str):
                tags.add(node["tag"])
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return tags


def _all_action_ids(card: dict) -> list[str]:
    return [v["action_id"] for v in _walk_callback_values(card) if v.get("action_id")]


def _assert_card_invariants(self: unittest.TestCase, out: dict, expect_interactive: bool = True) -> None:
    """The structural contract every compiled 2.0 card must satisfy."""
    self.assertTrue(out["ok"], out.get("error"))
    card = out["card"]
    self.assertEqual(card["schema"], "2.0")
    self.assertIn(card["header"]["template"], {"blue", "green", "red", "grey"})
    self.assertEqual(card["header"]["title"]["tag"], "plain_text")
    self.assertIsInstance(card["header"]["title"]["content"], str)
    self.assertTrue(card["header"]["title"]["content"])
    self.assertIsInstance(card["body"]["elements"], list)
    handlers = out["handlers"]
    self.assertIsInstance(handlers, dict)
    # 无死键:每个回调 value 的 action 都必须能在 handlers 里路由
    for v in _walk_callback_values(card):
        self.assertIsInstance(v.get("action"), str)
        self.assertRegex(v["action"], _ACTION_RE)
        self.assertIsInstance(v.get("round"), int)
        self.assertTrue(v.get("action_id"))
        self.assertIn(v["action"], handlers, f"action {v['action']} has no handler")
    for key, handler in handlers.items():
        self.assertTrue(_ACTION_RE.match(key) or _TODO_ACTION_RE.match(key), f"bad handler key {key!r}")
        self.assertIsInstance(handler, str)
        self.assertTrue(handler)
    # JSON 可往返(飞书通道只吃序列化后的卡片)
    json.dumps(card)
    json.dumps(handlers)
    if expect_interactive:
        self.assertTrue(_walk_tags(card) & {"button", "input"}, "expected interactive elements")
    else:
        self.assertFalse(_walk_tags(card) & {"button", "input"}, "terminal card must have no interactive elements")


REVIEW_XML = (
    '<card title="TODO 评价" template="blue">'
    '<info label="执行人" value="黄子建"/>'
    '<info label="任务" value="优化方案"/>'
    '<score min="1" max="5" rounds="20" bind-record="recX" selected="4"/>'
    '<comment placeholder="写点评语" bind-record="recX"/>'
    '<action-row><button text="打回重做" type="reject" action="review_reject"/></action-row>'
    "</card>"
)


# ── 1. 真实运行时集成(G03 验收固化为永久回归)────────────────────────────────


class TestRealRuntimeTodoCard(unittest.TestCase):
    """走真实 _todo_card_impl(非 stub):G03「发卡经模板、模板与手写版字节同构」."""

    def test_g03_build_todo_card_byte_isomorphic_with_template(self):
        # _build_todo_card 函数内延迟 `from _card_dsl import render_template`,取
        # sys.modules 当前版本。若其它 stub 套件在本文件之后被收集(命令行顺序),
        # 它们重导的 stub 版 _card_dsl 会覆盖 sys.modules,使本测试拿到 stub 渲染、
        # 字节不再同构。这里临时钉住本套件的真实 _card_dsl,测完恢复——任意收集
        # 顺序下 G03 都走真实模板。
        prev_card_dsl = sys.modules.get("_card_dsl")
        sys.modules["_card_dsl"] = dsl
        try:
            items = [
                {
                    "title": "写方案",
                    "task_guid": "g1",
                    "detail": "周五前",
                    "shape": "square",
                    "ledger_record_id": "recA",
                    "link": "https://doc.example/1",
                    "done": False,
                },
                {
                    "title": "评审",
                    "task_guid": "g2",
                    "detail": "",
                    "shape": "",
                    "ledger_record_id": "",
                    "link": "",
                    "done": True,
                },
                {
                    "title": "归档 🗂",
                    "task_guid": "g3",
                    "detail": '含引号 " 与尖括号 <b>',
                    "shape": "star",
                    "ledger_record_id": "recC",
                    "link": "",
                    "done": False,
                },
            ]
            title, subtitle, shape = "今日 TODO", "2026-08-27 · 三人组", "circle"
            tok, tbl = "app_tok", "tbl_id"
            card1, h1 = todo._build_todo_card(
                items=items,
                title=title,
                subtitle=subtitle,
                shape=shape,
                ledger_app_token=tok,
                ledger_table_id=tbl,
            )
            rows = [
                {
                    "title": i.get("title"),
                    "task_guid": i.get("task_guid"),
                    "detail": i.get("detail"),
                    "shape": i.get("shape"),
                    "ledger_record_id": i.get("ledger_record_id"),
                    "link": i.get("link"),
                    "done": i.get("done"),
                }
                for i in items
            ]
            out2 = dsl.render_template(
                "todo-card",
                values_json=json.dumps({"title": title, "rows": rows}, ensure_ascii=False),
                context_json=json.dumps(
                    {"subtitle": subtitle, "shape": shape, "ledger_app_token": tok, "ledger_table_id": tbl},
                    ensure_ascii=False,
                ),
            )
            self.assertTrue(out2["ok"], out2.get("error"))
            card2, h2 = out2["card"], out2["handlers"]
            # 发卡路径与模板直渲必须字节同构(acceptance G03)
            self.assertEqual(card1, card2)
            self.assertEqual(h1, h2)
        finally:
            if prev_card_dsl is None:
                sys.modules.pop("_card_dsl", None)
            else:
                sys.modules["_card_dsl"] = prev_card_dsl

    def test_todo_handlers_exact_per_row(self):
        out = dsl.render_template(
            "todo-card",
            values_json=json.dumps(
                {
                    "title": "T",
                    "rows": [
                        {"title": "a", "task_guid": "g", "done": False},
                        {"title": "b", "done": True},
                    ],
                },
                ensure_ascii=False,
            ),
        )
        self.assertTrue(out["ok"], out.get("error"))
        h = out["handlers"]
        # 未完成行 0:20 轮 tick + 20 轮 untick;已完成行 1:零 handler(只读)
        self.assertEqual({k for k in h if k.startswith("todo_tick_0_")}, {f"todo_tick_0_r{r}" for r in range(20)})
        self.assertEqual({k for k in h if k.startswith("todo_untick_0_")}, {f"todo_untick_0_r{r}" for r in range(20)})
        self.assertFalse(any(k.endswith("_1_") for k in h))

    def test_todo_link_wins_over_applink(self):
        card = todo._build_card_from_state(
            {
                "title": "T",
                "subtitle": "",
                "ledger_app_token": "",
                "ledger_table_id": "",
                "rows": [
                    {
                        "title": "带链接",
                        "task_guid": "g",
                        "link": "https://doc.example/x",
                        "done": False,
                        "round": 0,
                        "locked": False,
                    }
                ],
            }
        )
        text = json.dumps(card, ensure_ascii=False)
        self.assertIn("https://doc.example/x", text)
        self.assertNotIn("applink.feishu.cn", text)

    def test_todo_applink_fallback(self):
        card = todo._build_card_from_state(
            {
                "title": "T",
                "subtitle": "",
                "ledger_app_token": "",
                "ledger_table_id": "",
                "rows": [{"title": "任务", "task_guid": "g123", "done": False, "round": 0, "locked": False}],
            }
        )
        self.assertIn("applink.feishu.cn/client/todo/detail?guid=g123", json.dumps(card, ensure_ascii=False))

    def test_todo_progress_header_and_terminal_green(self):
        card = todo._build_card_from_state(
            {
                "title": "T",
                "subtitle": "子标题",
                "ledger_app_token": "",
                "ledger_table_id": "",
                "rows": [
                    {"title": "a", "done": True, "round": 0, "locked": True},
                    {"title": "b", "done": False, "round": 0, "locked": False},
                    {"title": "c", "done": True, "round": 0, "locked": True},
                ],
            }
        )
        header = card["elements"][0]["content"]
        self.assertIn("进度: 2/3 已完成", header)
        self.assertIn("子标题", header)
        self.assertEqual(card["header"]["template"], "blue")
        all_done = todo._build_card_from_state(
            {
                "title": "T",
                "subtitle": "",
                "ledger_app_token": "",
                "ledger_table_id": "",
                "rows": [{"title": "a", "done": True, "round": 0, "locked": True}],
            }
        )
        self.assertEqual(all_done["header"]["template"], "green")

    def test_todo_terminal_locks_rows_at_round_cap(self):
        # done 行在 round 19(最后一轮)不再给「撤销」按钮;未完成行在 round 20 不再给
        # 「标记完成」按钮 —— 轮次用尽即锁定,与引擎 2.0 卡终态只读同一语义。
        card = todo._build_card_from_state(
            {
                "title": "T",
                "subtitle": "",
                "ledger_app_token": "",
                "ledger_table_id": "",
                "rows": [
                    {"title": "done-19", "done": True, "round": 19, "locked": False},
                    {"title": "todo-20", "done": False, "round": 20, "locked": False},
                ],
            }
        )
        text = json.dumps(card, ensure_ascii=False)
        self.assertNotIn("撤销", text)
        self.assertNotIn("标记完成", text)

    def test_unknown_shape_falls_back_to_circle(self):
        self.assertEqual(todo._shape_chars("rainbow"), ("○", "●"))
        self.assertEqual(todo._shape_chars("star"), ("☆", "★"))

    def test_card_state_roundtrip(self):
        state = {"title": "T", "subtitle": "s", "rows": [{"title": "a", "done": True, "round": 1}]}
        blob = todo._serialize_card_state(state)
        parsed = todo._parse_card_state(blob)
        self.assertEqual(parsed, state)
        self.assertIsNone(todo._parse_card_state("not json"))
        self.assertIsNone(todo._parse_card_state('{"rows": "oops"}'))


def _review_kw(**overrides: dict) -> dict:
    """评价卡渲染的默认参数(工厂函数,避免可变类属性)."""
    kw = {
        "record_id": "recX",
        "title": "优化方案",
        "owner_name": "黄子建",
        "owner_open_id": "ou_1",
        "cycle_date": "2026-08-27",
        "task_guid": "g9",
        "selected_score": 3,
        "comment_value": "不错",
        "ledger_app_token": "tok",
        "ledger_table_id": "tbl",
        "note": "已打回重做",
        "round_": 2,
    }
    kw.update(overrides)
    return kw


class TestRealRuntimeReviewCard(unittest.TestCase):
    """评价卡生产路径(_render_review_card)与模板直渲一致,重建轮次语义正确."""

    def test_render_review_card_matches_template_direct(self):
        with mock.patch.object(review.time, "strftime", return_value="2026-08-27 12:00"):
            card1, h1 = review._render_review_card(**_review_kw())
        out2 = dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {
                    "owner_name": "黄子建",
                    "title": "优化方案",
                    "delivered_at": "2026-08-27 12:00",
                    "record_id": "recX",
                    "selected_score": 3,
                    "note": "已打回重做",
                },
                ensure_ascii=False,
            ),
            context_json=json.dumps(
                {
                    "owner_name": "黄子建",
                    "owner_open_id": "ou_1",
                    "cycle_date": "2026-08-27",
                    "task_guid": "g9",
                    "ledger_app_token": "tok",
                    "ledger_table_id": "tbl",
                    "comment_value": "不错",
                },
                ensure_ascii=False,
            ),
            round_=2,
        )
        self.assertTrue(out2["ok"], out2.get("error"))
        card2, h2 = out2["card"], out2["handlers"]
        self.assertEqual(card1, card2)
        self.assertEqual(h1, h2)

    def test_round_of_value_semantics(self):
        # 远端 e673c4b 的 _round_of_value(合并后取代自研 _next_round,更健壮):
        # 返回回调 value 携带的当前轮次,重建处 +1。
        self.assertEqual(review._round_of_value({"round": 0}), 0)
        self.assertEqual(review._round_of_value({"round": 5}), 5)
        self.assertEqual(review._round_of_value({"round": "7"}), 7)  # 字符串数字
        self.assertEqual(review._round_of_value({"round": True}), 0)  # bool 守卫
        self.assertEqual(review._round_of_value({}), 0)  # 缺省回退 action 名解析
        self.assertEqual(review._round_of_value({"round": "x"}), 0)  # 非法值回退
        self.assertEqual(review._round_of_value({"action": "review_score_r4"}), 4)  # action 名回退

    def test_rebuild_bumps_to_fresh_action_names(self):
        def names(r: int) -> set:
            card, _ = review._render_review_card(**_review_kw(round_=r))
            return {v["action"] for v in _walk_callback_values(card)}

        self.assertTrue(names(2).isdisjoint(names(3)))

    def test_terminal_rebuild_is_readonly(self):
        # 第 20 轮(round 19)点击后,重建轮次 = _round_of_value + 1 = 20 → 引擎渲染终态只读卡
        card, handlers = review._render_review_card(**_review_kw(round_=review._round_of_value({"round": 19}) + 1))
        tags = _walk_tags(card)
        self.assertNotIn("button", tags)
        self.assertNotIn("input", tags)
        self.assertEqual(handlers, {})
        # 信息行保留(执行人/任务/交付时间 + 评分 + 评语)
        contents = json.dumps(card, ensure_ascii=False)
        for expect in ("黄子建", "优化方案", "3 分", "不错"):
            self.assertIn(expect, contents)

    def test_score_highlight_survives_every_round(self):
        for r in range(20):
            card, _ = review._render_review_card(**_review_kw(selected_score=3, round_=r))
            self.assertIn("✓ 3分", json.dumps(card, ensure_ascii=False), f"round {r} lost highlight")


# ── 2. 结构不变量 ─────────────────────────────────────────────────────────────


class TestCardStructuralInvariants(unittest.TestCase):
    """编译产物必须满足的骨架契约(对每张卡都成立,不只是示例卡)。"""

    def test_review_invariants(self):
        out = dsl.render_card(REVIEW_XML, context_json='{"owner_name":"黄子建"}', round_=2)
        _assert_card_invariants(self, out)

    def test_empty_card_invariants(self):
        out = dsl.render_card('<card title="empty"/>')
        self.assertTrue(out["ok"])
        self.assertEqual(out["card"]["body"]["elements"], [])
        self.assertEqual(out["handlers"], {})

    def test_every_score_button_id_matches_scheme(self):
        out = dsl.render_card(
            '<card title="t"><score min="2" max="4" bind-record="r" action="grade"/></card>',
            handler_overrides_json='{"grade":"g"}',
        )
        ids = sorted(_all_action_ids(out["card"]))
        self.assertEqual(ids, ["grade_2_r0", "grade_3_r0", "grade_4_r0"])

    def test_score_button_count_and_score_value(self):
        out = dsl.render_card('<card title="t"><score min="1" max="5" bind-record="r"/></card>')
        cs = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")
        self.assertEqual(len(cs["columns"]), 5)
        for i, col in enumerate(cs["columns"]):
            v = col["elements"][0]["behaviors"][0]["value"]
            self.assertEqual(v["score"], i + 1)

    def test_no_interactive_element_without_callback(self):
        out = dsl.render_card(REVIEW_XML)
        for el in out["card"]["body"]["elements"]:
            if el.get("tag") in ("input",):
                self.assertIn("behaviors", el)
            if el.get("tag") == "column_set":
                for col in el.get("columns", []):
                    for sub in col.get("elements", []):
                        if sub.get("tag") == "button":
                            self.assertEqual(sub["behaviors"][0]["type"], "callback")

    def test_handlers_never_include_round_beyond_cap(self):
        for xml in (
            REVIEW_XML,
            '<card title="t"><comment bind-record="r"/></card>',
            '<card title="t"><action-row><button text="b" type="accept" action="go"/></action-row></card>',
        ):
            out = dsl.render_card(xml, handler_overrides_json='{"go":"g"}')
            for key in out["handlers"]:
                self.assertNotIn("_r20", key, f"{key} exceeds the 20-round cap")


# ── 3. handler 精确度 ─────────────────────────────────────────────────────────


class TestHandlerExactness(unittest.TestCase):
    """预注册深度与元素声明严格一致:不多不少,重复 action 不重复注册。"""

    def test_score_rounds_depth_exact(self):
        out = dsl.render_card('<card title="t"><score rounds="5" bind-record="r"/></card>')
        self.assertEqual({f"review_score_r{i}" for i in range(5)}, set(out["handlers"]))

    def test_comment_depth_always_20(self):
        out = dsl.render_card('<card title="t"><comment bind-record="r"/></card>')
        self.assertEqual({f"review_input_r{i}" for i in range(20)}, set(out["handlers"]))

    def test_button_depth_always_20(self):
        out = dsl.render_card(
            '<card title="t"><action-row><button text="b" type="accept" action="go"/></action-row></card>',
            handler_overrides_json='{"go":"g"}',
        )
        self.assertEqual({f"go_r{i}" for i in range(20)}, set(out["handlers"]))

    def test_mixed_card_total_is_sum_of_depths(self):
        out = dsl.render_card(REVIEW_XML)
        # score(20) + comment(20) + reject 按钮(20)
        self.assertEqual(len(out["handlers"]), 60)

    def test_duplicate_action_buttons_do_not_duplicate_handlers(self):
        xml = (
            '<card title="t"><action-row>'
            '<button text="a" type="accept" action="go"/><button text="b" type="accept" action="go"/>'
            "</action-row></card>"
        )
        out = dsl.render_card(xml, handler_overrides_json='{"go":"g"}')
        self.assertEqual(len(out["handlers"]), 20)  # 一个 action 只注册 20 轮
        ids = _all_action_ids(out["card"])
        self.assertEqual(len(ids), len(set(ids)))  # 但 action_id 必须互异


# ── 4. 轮次生命周期穷尽 ──────────────────────────────────────────────────────


class TestRoundLifecycleExhaustive(unittest.TestCase):
    """0..19 每一轮重建动作名互不撞车;round>=20 终态只读(修复后的契约)。"""

    def test_rebuild_names_disjoint_for_all_boundary_rounds(self):
        for r in range(19):
            a = {v["action"] for v in _walk_callback_values(dsl.render_card(REVIEW_XML, round_=r)["card"])}
            b = {v["action"] for v in _walk_callback_values(dsl.render_card(REVIEW_XML, round_=r + 1)["card"])}
            self.assertTrue(a.isdisjoint(b), f"round {r}→{r + 1} 动作名撞车: {a & b}")

    def test_all_20_rounds_have_handlers(self):
        for r in range(20):
            out = dsl.render_card(REVIEW_XML, round_=r)
            for v in _walk_callback_values(out["card"]):
                self.assertIn(v["action"], out["handlers"], f"round {r}: {v['action']} 无 handler")

    def test_terminal_round_20_and_beyond_readonly(self):
        for r in (20, 21, 99, 1000):
            out = dsl.render_card(REVIEW_XML, round_=r)
            _assert_card_invariants(self, out, expect_interactive=False)
            self.assertEqual(out["handlers"], {})

    def test_terminal_card_keeps_info_lines(self):
        out = dsl.render_card(REVIEW_XML, round_=99, context_json='{"comment_value":"收尾评语"}')
        md = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertEqual(len(md), 4)  # 2 info + 评分 + 评语
        joined = "|".join(e["content"] for e in md)
        self.assertIn("4 分", joined)  # selected="4" 在终态以文本呈现
        self.assertIn("收尾评语", joined)

    def test_negative_and_junk_round_still_round_0(self):
        for bad in (-5, "abc", None, 0, 19):
            out = dsl.render_card(REVIEW_XML, round_=bad)
            if isinstance(bad, int) and bad >= 20:
                continue
            names = {v["action"] for v in _walk_callback_values(out["card"])}
            expect = "r19" if bad == 19 else "r0"
            self.assertTrue(all(n.endswith(f"_{expect}") for n in names), f"round_={bad!r} → {sorted(names)}")

    def test_two_scores_same_action_no_collision(self):
        xml = (
            '<card title="t"><score min="1" max="2" action="grade" bind-record="r1"/>'
            '<score min="1" max="2" action="grade" bind-record="r2"/></card>'
        )
        out = dsl.render_card(xml, handler_overrides_json='{"grade":"g"}')
        ids = _all_action_ids(out["card"])
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(ids), len(set(ids)), f"同 action 双评分组 action_id 撞车: {ids}")


# ── 5. 组合矩阵 + XSD 对齐 ────────────────────────────────────────────────────

_PARTS = {
    "info": '<info label="L" value="V"/>',
    "score": '<score min="1" max="3" rounds="3" bind-record="r"/>',
    "comment": '<comment bind-record="r"/>',
    "action-row": '<action-row><button text="B" type="accept" action="go"/></action-row>',
}


class TestCombinatorialMatrix(unittest.TestCase):
    """4 模板 x 16 种子组合 = 64 张合法卡,全部编译成功且过结构不变量。"""

    def test_all_64_combinations_compile(self):
        for template in ("blue", "green", "red", "grey"):
            for mask in range(16):
                children = [_PARTS[k] for i, k in enumerate(_PARTS) if mask & (1 << i)]
                xml = f'<card title="t" template="{template}">{"".join(children)}</card>'
                out = dsl.render_card(xml, handler_overrides_json='{"go":"g"}')
                # 只有 info 的组合(0b0001)没有交互元素,其余组合都带 score/comment/button
                _assert_card_invariants(self, out, expect_interactive=(mask & 0b1110) != 0)

    def test_invalid_matrix_all_fail_closed(self):
        cases = [
            '<card><info label="a" value="b"/></card>',
            '<info label="a" value="b"/>',
            '<card title="x"><info></card>',
            '<card title="x" template="purple"/>',
            '<card title="x"><foo/></card>',
            '<card title="x"><action-row><button text="a" type="rainbow" action="go"/></action-row></card>',
            '<card title="x"><action-row><button text="a"/></action-row></card>',
            '<card title="x"><action-row/></card>',
            '<card title="x"><info label="only"/></card>',
            '<card title="x"><list><row title="a"/></list><list><row title="b"/></list></card>',
            '<card title="x"><list><row title="a"/></list><score bind-record="r"/></card>',
            '<card title="x"><action-row><button text="a" action="mystery"/></action-row></card>',
        ]
        for xml in cases:
            out = dsl.render_card(xml)
            self.assertFalse(out["ok"], f"should fail closed: {xml}")
            self.assertIsInstance(out["error"], str)

    def test_valid_matrix_xsd_agreement(self):
        if xmlschema is None:
            self.skipTest("xmlschema not installed")
        xsd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "card.xsd")
        schema = xmlschema.XMLSchema(xsd_path)
        for template in ("blue", "green", "grey"):
            for mask in (0b0001, 0b0110, 0b1010, 0b1111):
                children = [_PARTS[k] for i, k in enumerate(_PARTS) if mask & (1 << i)]
                xml = f'<card title="t" template="{template}">{"".join(children)}</card>'
                self.assertTrue(schema.is_valid(xml), f"XSD rejects valid combo: {xml}")
                out = dsl.render_card(xml, handler_overrides_json='{"go":"g"}')
                self.assertTrue(out["ok"])

    def test_engine_stricter_than_xsd_on_multiple_lists(self):
        # 已知的刻意分歧: XSD choice 允许多个 <list>, 引擎拒绝(第二个会静默丢行)。
        if xmlschema is None:
            self.skipTest("xmlschema not installed")
        xsd_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "card.xsd")
        schema = xmlschema.XMLSchema(xsd_path)
        xml = '<card title="t"><list><row title="a"/></list><list><row title="b"/></list></card>'
        self.assertTrue(schema.is_valid(xml))
        self.assertFalse(dsl.render_card(xml)["ok"])


# ── 6. 确定性 ────────────────────────────────────────────────────────────────


class TestDeterminismAndPurity(unittest.TestCase):
    """同输入必同输出;调用不产生副作用;输入不被改写。"""

    def test_render_card_is_deterministic(self):
        a = dsl.render_card(REVIEW_XML, context_json='{"owner_name":"黄子建"}', round_=3)
        b = dsl.render_card(REVIEW_XML, context_json='{"owner_name":"黄子建"}', round_=3)
        self.assertEqual(a, b)

    def test_render_template_is_deterministic(self):
        vals = json.dumps(
            {"owner_name": "黄子建", "title": "T", "delivered_at": "d", "record_id": "r", "selected_score": 2},
            ensure_ascii=False,
        )
        a = dsl.render_template("review-card", values_json=vals)
        b = dsl.render_template("review-card", values_json=vals)
        self.assertEqual(a, b)

    def test_dict_arguments_accepted_and_not_mutated(self):
        values = {"owner_name": "黄子建", "title": "T", "delivered_at": "d", "record_id": "r", "selected_score": 0}
        snapshot = json.dumps(values, sort_keys=True)
        out = dsl.render_template("review-card", values_json=values)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(json.dumps(values, sort_keys=True), snapshot)  # 输入未被改写

    def test_render_purity_identical_calls(self):
        a = dsl.render_card(REVIEW_XML)
        b = dsl.render_card(REVIEW_XML)
        self.assertEqual(a, b)  # 两次调用无副作用(ring 6)


# ── 7. 对抗性模板填充 ────────────────────────────────────────────────────────


class TestAdversarialTemplateFill(unittest.TestCase):
    """值里的 XML 元字符 / 占位符形状文本都不得破坏结构或二次展开(单遍替换)。"""

    def test_todo_row_title_with_xml_metachars_fails_closed(self):
        # 值里的 {note}/{rows} 形状文本会被「未填充占位符」扫描误报——这是引擎文档
        # 写明的取舍(报错可修、脏值静默产错不可修):fail-closed,绝不放脏卡。
        # XML 元字符本身(引号/尖括号/&)不触发误报,单遍替换不会二次展开。
        evil = "\"'<b>&amp;"
        out = dsl.render_template(
            "todo-card",
            values_json=json.dumps({"title": "T", "rows": [{"title": evil, "task_guid": "g"}]}, ensure_ascii=False),
        )
        self.assertTrue(out["ok"], out.get("error"))
        text = json.dumps(out["card"], ensure_ascii=False)
        self.assertIn("<b>", text)  # 字面量保留(未二次展开)
        json.dumps(out["card"])  # JSON 可往返 → 结构没被注入破坏
        # 含 {note} 的值 → 明确报错而非发脏卡
        out2 = dsl.render_template(
            "todo-card",
            values_json=json.dumps(
                {"title": "T", "rows": [{"title": "a {note} b", "task_guid": "g"}]},
                ensure_ascii=False,
            ),
        )
        self.assertFalse(out2["ok"])
        self.assertIn("未填充", out2["error"])

    def test_review_value_with_placeholder_shaped_text_fails_closed(self):
        # 值里含 {note}/{rows} 字面量 → 未填充占位符扫描误报 → fail-closed(不放脏卡),
        # 这是引擎文档声明的取舍;普通 XML 元字符值不受影响、单遍替换不二次展开。
        out = dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {
                    "owner_name": "a {note} b",
                    "title": "c {rows} d",
                    "delivered_at": "d",
                    "record_id": "r",
                    "selected_score": 0,
                },
                ensure_ascii=False,
            ),
        )
        self.assertFalse(out["ok"])
        self.assertIn("未填充", out["error"])
        self.assertIn("note", out["error"])
        # 无占位符形状的值:正常编译
        out_ok = dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {
                    "owner_name": 'He said "hi" <b>',
                    "title": "T",
                    "delivered_at": "d",
                    "record_id": "r",
                    "selected_score": 0,
                },
                ensure_ascii=False,
            ),
        )
        self.assertTrue(out_ok["ok"], out_ok.get("error"))

    def test_rows_not_a_list_fails_closed(self):
        out = dsl.render_template("todo-card", values_json='{"title":"T","rows":"oops"}')
        self.assertFalse(out["ok"])
        self.assertIn("rows", out["error"])

    def test_all_metachars_through_review_template(self):
        evil = "<>&\"'"
        out = dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {"owner_name": evil, "title": evil, "delivered_at": evil, "record_id": evil, "selected_score": 0},
                ensure_ascii=False,
            ),
        )
        self.assertTrue(out["ok"], out.get("error"))
        json.dumps(out["card"])

    def test_filled_review_card_parses_as_xml(self):
        # 模板填充后的 XML 必须仍是合法 XML(转义闭环:填充 → ET 解析成功)
        filled = dsl._fill_template(
            '<card title="{t}"><info label="{l}" value="{v}"/></card>',
            {"t": 'a "q" <b> &', "l": "L", "v": "v"},
        )
        root = ET.fromstring(filled)
        self.assertEqual(root.get("title"), 'a "q" <b> &')


# ── 8. 跨模块一致性(防止三处轮次上限悄悄漂移)────────────────────────────────


class TestCrossModuleConsistency(unittest.TestCase):
    """三个模块各自声明的轮次上限必须一致;内置 handler 别名与文档一致。"""

    def test_round_ceilings_agree(self):
        self.assertEqual(dsl._MAX_ROUNDS, 20)
        self.assertEqual(review._MAX_ROUNDS, dsl._MAX_ROUNDS)
        self.assertEqual(todo._UNDO_ROUNDS, dsl._MAX_ROUNDS)

    def test_doc_alias_reject_builtin(self):
        # 文档示例 action="reject" 无需 overrides 即可编译(上一轮修复,固化为回归)
        self.assertEqual(dsl._BUILTIN_HANDLERS["reject"], "feishu_review_reject")
        out = dsl.render_card(
            '<card title="t"><action-row><button text="打回" type="reject" action="reject"/></action-row></card>'
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["handlers"]["reject_r0"], "feishu_review_reject")

    def test_review_template_rounds_matches_engine_cap(self):
        # 模板声明 rounds="20" 必须与引擎上限一致,否则预注册深度 < 重建轮次 → 死键
        tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "templates")
        with open(os.path.join(tdir, "review-card.xml"), encoding="utf-8") as fh:
            xml = fh.read()
        self.assertIn('rounds="20"', xml)


if __name__ == "__main__":
    # 必须放文件末尾(unittest.main() 执行即退出,放在中间会截断后续测试类)
    unittest.main(verbosity=2)
