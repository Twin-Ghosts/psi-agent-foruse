"""Hardening / adversarial tests for the card DSL engine (_card_dsl).

The DSL is fed by an LLM (海豚) and business code, i.e. semi-trusted input, so
this suite pushes the untrusted-input and rebuild-invariant edges that the unit /
spec / fixes suites don't: XML entity attacks, serialization invariants, the
``rounds`` vs round clamp interaction (dead buttons on rebuild), XSD boolean
drift, and click-target routability under stress.

Run:  PYTHONPATH=. python -m pytest _test_card_dsl_hardening.py -q
  or: PYTHONPATH=. python _test_card_dsl_hardening.py
"""

from __future__ import annotations

import json
import os
import sys
import types
import typing
import unittest
from typing import Any

# ── stub runtime deps (superset shape, order-independent across suites) ──────
_impl = types.ModuleType("_todo_card_impl")
_impl._UNDO_ROUNDS = 20
_impl._build_card_from_state = lambda state: {"schema": "2.0", "_state": state, "_legacy_state": state}
_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
# 收集顺序无关:强制安装本套件的 stub(与其余 stub 套件形状一致),使 _card_dsl
# 的模块级绑定不依赖 pytest 的导入顺序——否则 strict 套件(真实运行时)先被收集
# 时,_card_dsl 会绑定真实 _build_card_from_state,本套件对 _state 形状的断言挂掉。
sys.modules.pop("_todo_card_impl", None)
sys.modules.pop("_runtime_paths", None)
sys.modules.pop("_card_dsl", None)
sys.modules["_todo_card_impl"] = _impl

_paths = types.ModuleType("_runtime_paths")
_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules["_runtime_paths"] = _paths

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl  # noqa: E402


def _walk_values(card: dict[str, Any]):
    """Yield every callback value embedded in the compiled card."""

    def walk(node: Any):
        if isinstance(node, dict):
            if node.get("tag") in ("button", "input"):
                for b in node.get("behaviors", []):
                    yield b["value"]
            for v in node.values():
                yield from walk(v)
        elif isinstance(node, list):
            for v in node:
                yield from walk(v)

    yield from walk(card)


def _click_targets(card: dict[str, Any]) -> set[str]:
    return {v["action"] for v in _walk_values(card)}


# ── 1. Untrusted XML: entity / injection attacks ─────────────────────────────
class TestUntrustedXml(unittest.TestCase):
    def test_xxe_external_entity_blocked(self):
        # External entities must never be resolved (would leak local files).
        xxe = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE card [ <!ENTITY x SYSTEM "file:///etc/hostname"> ]>'
            '<card title="&x;"><info label="a" value="b"/></card>'
        )
        out = _card_dsl.render_card(xxe)
        self.assertFalse(out["ok"])
        self.assertIn("not valid XML", out["error"])

    def test_billion_laughs_amplification_bounded(self):
        # 6 levels x10 => 10^6 expansion from a tiny doc; the parser must refuse,
        # not blow up memory. (Python 3.14 expat caps the amplification factor.)
        defs = ['<!ENTITY lol0 "haha">']
        for i in range(1, 7):
            ref = f"&lol{i - 1};" * 10
            defs.append(f'<!ENTITY lol{i} "{ref}">')
        doc = (
            '<?xml version="1.0"?>\n<!DOCTYPE card [\n'
            + "\n".join(defs)
            + '\n]>\n<card title="&lol6;"><info label="a" value="b"/></card>'
        )
        out = _card_dsl.render_card(doc)
        self.assertFalse(out["ok"], "billion-laughs must not compile")
        self.assertIn("not valid XML", out["error"])

    def test_undefined_entity_rejected(self):
        out = _card_dsl.render_card('<card title="&nope;"><info label="a" value="b"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("not valid XML", out["error"])

    def test_small_internal_entity_passthrough_is_escaped_in_json(self):
        # A single benign internal entity is allowed to expand; its value must
        # survive into the card as plain text (no structural leakage).
        doc = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE card [<!ENTITY a "A&amp;B">]>'
            '<card title="&a;"><info label="x" value="y"/></card>'
        )
        out = _card_dsl.render_card(doc)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["title"]["content"], "A&B")
        json.dumps(out["card"])  # round-trips

    def test_attribute_with_quotes_and_markup_never_breaks_json(self):
        # Values carrying " ' < > & { } must land as data, never as structure.
        nasty = "he said \"hi\" & <b>{record_id}</b> 'x'"
        out = _card_dsl.render_card(f'<card title="{_card_dsl._xml_escape(nasty)}"><info label="l" value="v"/></card>')
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["title"]["content"], nasty)
        blob = json.dumps(out["card"])
        self.assertIn("record_id", blob)  # present as literal text, not expanded


# ── 2. Rebuild invariant: no dead buttons at any round (rounds vs clamp) ─────
class TestNoDeadButtonOnRebuild(unittest.TestCase):
    def test_short_rounds_rebuilt_past_depth_still_routable(self):
        # <score rounds="5"> rebuilt to round 5+ used to render review_score_r5
        # with no handler → a dead button. Every rendered click target must have
        # a handler at every reachable round.
        xml = '<card title="t"><score min="1" max="3" rounds="5"/></card>'
        for rnd in (0, 4, 5, 6, 10, 19, 25):
            out = _card_dsl.render_card(xml, round_=rnd)
            self.assertTrue(out["ok"], out.get("error"))
            dead = _click_targets(out["card"]) - set(out["handlers"])
            self.assertEqual(dead, set(), f"round_={rnd} dead buttons: {dead}")

    def test_rounds_is_minimum_preregistration_depth(self):
        # rounds still governs the *minimum* handler depth at round 0.
        out = _card_dsl.render_card('<card title="t"><score rounds="5"/></card>', round_=0)
        self.assertIn("review_score_r4", out["handlers"])
        self.assertNotIn("review_score_r5", out["handlers"])

    def test_every_reachable_round_routable_default_card(self):
        review = (
            '<card title="t"><info label="a" value="b"/>'
            '<score min="1" max="5" bind-record="recX"/>'
            '<comment bind-record="recX"/>'
            '<action-row><button text="打回" type="reject" action="review_reject"/></action-row></card>'
        )
        for rnd in range(0, 20):
            out = _card_dsl.render_card(review, round_=rnd)
            self.assertTrue(out["ok"], out.get("error"))
            dead = _click_targets(out["card"]) - set(out["handlers"])
            self.assertEqual(dead, set(), f"round_={rnd} dead: {dead}")


# ── 3. XSD boolean drift: <row done="..."> ───────────────────────────────────
class TestDoneBooleanMatchesXsd(unittest.TestCase):
    def _done(self, literal: str) -> bool:
        out = _card_dsl.render_card(f'<card title="t"><list><row title="r" done="{literal}"/></list></card>')
        self.assertTrue(out["ok"], out.get("error"))
        # 真实 todo 卡:done 行只读(零 handler);未完成行有 20 轮 tick 预注册。
        return not any(k.startswith("todo_tick_0_") for k in out["handlers"])

    def test_xsd_true_literals_are_done(self):
        # xs:boolean true set: "true"/"1" (plus common LLM casings tolerated).
        for lit in ("true", "TRUE", "True", "1"):
            self.assertTrue(self._done(lit), f"done={lit!r} should be done")

    def test_xsd_false_literals_are_not_done(self):
        for lit in ("false", "0", "", "no", "yes"):
            self.assertFalse(self._done(lit), f"done={lit!r} should not be done")

    def test_done_row_is_locked(self):
        out = _card_dsl.render_card('<card title="t"><list><row title="r" done="1"/></list></card>')
        # locked=done 行发卡即只读:不产生任何 tick/untick handler(卡面无按钮)
        self.assertFalse(any(k.startswith("todo_") for k in out["handlers"]))


# ── 4. Serialization invariant: card + handlers are always JSON-clean ────────
class TestSerializationInvariant(unittest.TestCase):
    CARDS = (
        '<card title="眉" template="green"><info label="l" value="v"/></card>',
        '<card title="t"><score min="2" max="8" rounds="3" bind-record="r" selected="4"/></card>',
        '<card title="t"><comment placeholder="写点" bind-record="r"/><comment bind-record="r"/></card>',
        '<card title="t"><action-row><button text="通过" type="accept" action="review_reject"/>'
        '<button text="再" type="primary" action="review_reject"/></action-row></card>',
        '<card title="t"><list><row title="a" done="1"/><row title="b" bind-record="r2"/></list></card>',
    )

    def test_every_card_json_roundtrips(self):
        for xml in self.CARDS:
            out = _card_dsl.render_card(xml)
            self.assertTrue(out["ok"], f"{xml[:40]}: {out.get('error')}")
            # 序列化不变量:card 与 handlers 均 JSON 往返无损。真实 todo 卡顶层
            # 是 config/header/elements 结构(无 schema 键),普通卡有 schema=2.0,
            # 统一断言「往返后与原值相等」即覆盖两种形状。
            reloaded = json.loads(json.dumps(out["card"], ensure_ascii=False))
            self.assertEqual(reloaded, out["card"])
            # handlers dict is str->str and JSON-clean too
            for k, v in out["handlers"].items():
                self.assertIsInstance(k, str)
                self.assertIsInstance(v, str)

    def test_handlers_keys_are_unique_and_round_named(self):
        out = _card_dsl.render_card('<card title="t"><score rounds="6"/></card>')
        keys = list(out["handlers"])
        self.assertEqual(len(keys), len(set(keys)), "handler keys must be unique")
        self.assertTrue(all(k.startswith("review_score_r") for k in keys))


# ── 5. Routability property: no rendered value ever points nowhere ───────────
class TestRoutabilityProperty(unittest.TestCase):
    SAMPLES = (
        '<card title="t"><score min="1" max="9" rounds="1"/></card>',
        '<card title="t"><score action="grade" rounds="2"/></card>',
        '<card title="t"><comment action="note"/></card>',
        '<card title="t"><action-row><button text="x" action="approve"/>'
        '<button text="y" action="approve"/></action-row></card>',
    )
    OVERRIDES = json.dumps({"grade": "g", "note": "n", "approve": "a"})

    def test_no_dead_targets_across_rounds(self):
        for xml in self.SAMPLES:
            for rnd in (0, 1, 2, 3, 19, 30):
                out = _card_dsl.render_card(xml, round_=rnd, handler_overrides_json=self.OVERRIDES)
                self.assertTrue(out["ok"], f"{xml[:40]} r{rnd}: {out.get('error')}")
                dead = _click_targets(out["card"]) - set(out["handlers"])
                self.assertEqual(dead, set(), f"{xml[:40]} r{rnd} dead: {dead}")


# ── 6. comment confirm 文案可覆盖(默认回退评价卡措辞)────────────────────────
class TestCommentConfirmText(unittest.TestCase):
    def _confirm(self, xml: str) -> dict[str, Any]:
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"], out.get("error"))
        for el in out["card"]["body"]["elements"]:
            if el.get("tag") == "input":
                return el["confirm"]
        raise AssertionError("no input element found")

    def test_default_confirm_text_is_review_wording(self):
        c = self._confirm('<card title="t"><comment bind-record="r"/></card>')
        self.assertEqual(c["title"]["content"], "确认评语")
        self.assertIn("写入台账", c["text"]["content"])

    def test_confirm_text_overridable_for_other_card_types(self):
        # 审批卡自声明文案,不复用评价卡「写入台账」措辞(T-113 观察点)。
        c = self._confirm(
            '<card title="t"><comment bind-record="r" '
            'confirm-title="确认驳回意见" confirm-text="提交这条审批意见?"/></card>'
        )
        self.assertEqual(c["title"]["content"], "确认驳回意见")
        self.assertEqual(c["text"]["content"], "提交这条审批意见?")

    def test_confirm_text_with_quotes_survives_json(self):
        c = self._confirm(
            '<card title="t"><comment bind-record="r" confirm-text="写入&quot;台账&quot;?"/></card>'
        )
        self.assertEqual(c["text"]["content"], '写入"台账"?')


# ── 7. 轮次上限单一真源:_card_dsl 侧与 todo stub 相等(真实三模块一致性由
#      strict 套件在真实运行时断言,此处只钉 stub 环境可验的 _card_dsl 一侧)──
class TestMaxRoundsSingleSource(unittest.TestCase):
    def test_card_dsl_ceiling_sourced_from_undo_rounds(self):
        # _MAX_ROUNDS = _UNDO_ROUNDS(单一真源);stub 里 _UNDO_ROUNDS=20。
        self.assertEqual(_card_dsl._MAX_ROUNDS, _impl._UNDO_ROUNDS)


# ── 8. divider 元素(飞书 2.0 hr,实卡验证通过)─────────────────────────────────
class TestDivider(unittest.TestCase):
    def test_divider_compiles_to_hr(self):
        out = _card_dsl.render_card(
            '<card title="t"><info label="a" value="b"/><divider/>'
            '<info label="c" value="d"/></card>'
        )
        self.assertTrue(out["ok"], out.get("error"))
        tags = [e["tag"] for e in out["card"]["body"]["elements"]]
        self.assertEqual(tags, ["markdown", "hr", "markdown"])

    def test_note_is_rejected_as_unknown(self):
        # note 在飞书 2.0 已移除,不在词汇表内,应 fail-closed(而非编译出脏卡)。
        out = _card_dsl.render_card('<card title="t"><note text="x"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("unknown element", out["error"])


# ── 9. 新增交互/展示元素(button confirm / img / date / select),实卡验证通过 ──
class TestNewElements(unittest.TestCase):
    def _first(self, card, tag):
        def walk(n):
            if isinstance(n, dict):
                if n.get("tag") == tag:
                    yield n
                for v in n.values():
                    yield from walk(v)
            elif isinstance(n, list):
                for v in n:
                    yield from walk(v)
        return next(walk(card), None)

    def test_button_confirm(self):
        out = _card_dsl.render_card(
            '<card title="t"><action-row>'
            '<button text="打回" type="reject" action="review_reject" '
            'confirm="确定打回?" confirm-title="确认打回"/></action-row></card>'
        )
        self.assertTrue(out["ok"], out.get("error"))
        btn = self._first(out["card"], "button")
        self.assertEqual(btn["confirm"]["title"]["content"], "确认打回")
        self.assertEqual(btn["confirm"]["text"]["content"], "确定打回?")

    def test_button_without_confirm_has_no_confirm_field(self):
        out = _card_dsl.render_card(
            '<card title="t"><action-row><button text="x" action="review_reject"/></action-row></card>'
        )
        self.assertNotIn("confirm", self._first(out["card"], "button"))

    def test_img_requires_key(self):
        out = _card_dsl.render_card('<card title="t"><img alt="x"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("img-key", out["error"])

    def test_img_compiles(self):
        out = _card_dsl.render_card('<card title="t"><img img-key="img_v3_x" alt="图"/></card>')
        self.assertTrue(out["ok"], out.get("error"))
        img = self._first(out["card"], "img")
        self.assertEqual(img["img_key"], "img_v3_x")

    def test_date_picker(self):
        out = _card_dsl.render_card(
            '<card title="t"><date action="pick_date" placeholder="选日期"/></card>',
            handler_overrides_json='{"pick_date":"h"}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        dp = self._first(out["card"], "date_picker")
        self.assertEqual(dp["behaviors"][0]["value"]["action"], "pick_date_r0")
        self.assertIn("pick_date_r0", out["handlers"])

    def test_date_requires_action(self):
        out = _card_dsl.render_card('<card title="t"><date placeholder="x"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("action", out["error"])

    def test_select_with_options(self):
        out = _card_dsl.render_card(
            '<card title="t"><select action="pick" placeholder="选">'
            '<option text="甲" value="a"/><option text="乙" value="b"/></select></card>',
            handler_overrides_json='{"pick":"h"}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        sel = self._first(out["card"], "select_static")
        self.assertEqual([o["value"] for o in sel["options"]], ["a", "b"])
        self.assertIn("pick_r0", out["handlers"])

    def test_select_needs_at_least_one_option(self):
        out = _card_dsl.render_card(
            '<card title="t"><select action="pick"/></card>', handler_overrides_json='{"pick":"h"}'
        )
        self.assertFalse(out["ok"])
        self.assertIn("option", out["error"])

    def test_option_requires_text_and_value(self):
        out = _card_dsl.render_card(
            '<card title="t"><select action="pick"><option text="甲"/></select></card>',
            handler_overrides_json='{"pick":"h"}',
        )
        self.assertFalse(out["ok"])


# ── 10. table 元素:Bitable 数据 → 飞书原生 table 组件(实卡验证通过)──────────
class TestTableElement(unittest.TestCase):
    ROWS: typing.ClassVar = [
        {"record_id": "r1", "fields": {"任务": "A", "负责人": "张三", "标签": [{"text": "高优"}, {"text": "本周"}]}},
        {"record_id": "r2", "fields": {"任务": "B", "负责人": "李四", "标签": [{"text": "常规"}]}},
    ]

    def _table(self, card):
        return next(e for e in card["body"]["elements"] if e.get("tag") == "table")

    def test_renders_native_table_with_columns_and_rows(self):
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows">'
            '<col field="任务" label="任务"/><col field="负责人" label="负责人"/>'
            '<col field="标签" label="标签"/></table></card>',
            context_json=json.dumps({"rows": self.ROWS}, ensure_ascii=False),
        )
        self.assertTrue(out["ok"], out.get("error"))
        tbl = self._table(out["card"])
        self.assertEqual([c["display_name"] for c in tbl["columns"]], ["任务", "负责人", "标签"])
        self.assertEqual(len(tbl["rows"]), 2)
        # 数组字段(多选)拼成顿号分隔文本
        self.assertEqual(tbl["rows"][0]["c2"], "高优、本周")

    def test_empty_source_shows_empty_text(self):
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows" empty="本周无任务">'
            '<col field="任务"/></table></card>',
            context_json='{"rows":[]}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        md = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("本周无任务" in e["content"] for e in md))

    def test_col_only_holds_col(self):
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows"><info label="a" value="b"/></table></card>',
            context_json=json.dumps({"rows": self.ROWS}, ensure_ascii=False),
        )
        self.assertFalse(out["ok"])
        self.assertIn("col", out["error"])

    def test_missing_field_cell_is_dash(self):
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows"><col field="不存在字段"/></table></card>',
            context_json=json.dumps({"rows": self.ROWS}, ensure_ascii=False),
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(self._table(out["card"])["rows"][0]["c0"], "—")


if __name__ == "__main__":
    # Run the *whole* module (all TestCase classes), not a hand-picked subset —
    # unittest.main() discovers every class here, so this stays in sync as
    # classes are added. (Guards the "only half the tests ran" trap.)
    unittest.main(verbosity=2)
