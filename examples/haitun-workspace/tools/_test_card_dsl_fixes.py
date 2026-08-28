"""Regression tests for the six issues found in the 2026-08-27 adversarial review.

A. multiple <list> elements silently dropped rows      → now a compile error
B. two buttons with the same action collided action_id  → occurrence folded in
C. _row_xml ignored the ledger_record_id key            → both key names accepted
D. template missing a key left a dirty {record_id}      → leftover placeholders error
E. <score rounds="N"> was ignored (hardcoded 20)        → rounds honoured
F. two comments with the same action shared action_id   → occurrence folded in
"""

import json
import os
import sys
import types
import unittest

# ── stub the runtime deps exactly like the spec suite (list cards expose the
#    state blob through _build_card_from_state; we assert on _legacy_state) ────
_impl = types.ModuleType("_todo_card_impl")
_impl._UNDO_ROUNDS = 20
_impl._build_card_from_state = lambda state: {"schema": "2.0", "_legacy_state": state, "_state": state}
_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
# 收集顺序无关:强制安装本套件的 stub(与其余 stub 套件形状一致),使 _card_dsl
# 的模块级绑定不依赖 pytest 的导入顺序(strict 套件会再 pop 重导真实实现)。
sys.modules.pop("_todo_card_impl", None)
sys.modules.pop("_runtime_paths", None)
sys.modules.pop("_card_dsl", None)
sys.modules["_todo_card_impl"] = _impl

_paths = types.ModuleType("_runtime_paths")
_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules["_runtime_paths"] = _paths

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl  # noqa: E402
from _test_card_dsl import REVIEW_XML  # noqa: E402  (reuse the canonical review-card XML)


def _interactive(out, tag):
    """Return (element, callback-value) for every interactive element with ``tag``."""
    found = []
    for el in out["card"]["body"]["elements"]:
        if el.get("tag") == "column_set":
            for col in el.get("columns", []):
                for sub in col.get("elements", []):
                    if sub.get("tag") == tag:
                        found.append((sub, sub["behaviors"][0]["value"]))
        elif el.get("tag") == tag:
            found.append((el, el["behaviors"][0]["value"]))
    return found


class TestMultipleLists(unittest.TestCase):
    def test_two_lists_is_an_error(self):
        xml = "<card title='t'><list><row title='a'/></list><list><row title='b'/></list></card>"
        out = _card_dsl.render_card(xml)
        self.assertFalse(out["ok"], "two <list> elements must not compile")
        self.assertIn("只能有一个 <list>", out["error"])

    def test_one_list_still_works(self):
        out = _card_dsl.render_card("<card title='t'><list><row title='a'/></list></card>")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(len(out["card"]["_legacy_state"]["rows"]), 1)


class TestSameActionButtons(unittest.TestCase):
    def test_second_button_action_id_is_distinct(self):
        xml = (
            "<card title='t'>"
            "<action-row>"
            "<button text='通过' type='accept' action='go'/>"
            "<button text='再通过' type='accept' action='go'/>"
            "</action-row></card>"
        )
        out = _card_dsl.render_card(xml, handler_overrides_json='{"go":"my_tool"}')
        self.assertTrue(out["ok"], out.get("error"))
        buttons = _interactive(out, "button")
        self.assertEqual(len(buttons), 2)
        v0, v1 = buttons[0][1], buttons[1][1]
        self.assertEqual(v0["action_id"], "go_r0")
        self.assertEqual(v1["action_id"], "go_1_r0")
        self.assertNotEqual(v0["action_id"], v1["action_id"])
        # both still route through the same handler by action name
        self.assertEqual(out["handlers"]["go_r0"], "my_tool")

    def test_single_button_keeps_original_action_id(self):
        xml = (
            "<card title='t'><action-row><button text='打回' type='reject' action='review_reject'/></action-row></card>"
        )
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"], out.get("error"))
        buttons = _interactive(out, "button")
        self.assertEqual(buttons[0][1]["action_id"], "review_reject_r0")


class TestRowXmlKeyAlias(unittest.TestCase):
    def test_ledger_record_id_is_accepted_in_rows(self):
        out = _card_dsl.render_template(
            "todo-card",
            values_json=json.dumps(
                {"title": "T", "rows": [{"title": "a", "ledger_record_id": "r9"}]},
                ensure_ascii=False,
            ),
            context_json='{"ledger_app_token":"tok","ledger_table_id":"tbl"}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        rows = out["card"]["_legacy_state"]["rows"]
        self.assertEqual(rows[0]["ledger_record_id"], "r9")

    def test_bind_record_still_wins(self):
        row_xml = _card_dsl._row_xml({"title": "a", "bind_record": "r1", "ledger_record_id": "r2"})
        self.assertIn('bind-record="r1"', row_xml)
        self.assertNotIn("r2", row_xml)


class TestDirtyPlaceholder(unittest.TestCase):
    def test_missing_record_id_is_an_error(self):
        out = _card_dsl.render_template(
            "review-card",
            values_json='{"owner_name":"a","title":"b","delivered_at":"c","selected_score":0}',
        )
        self.assertFalse(out["ok"], "missing record_id must not compile")
        self.assertIn("record_id", out["error"])
        self.assertIn("未填充", out["error"])

    def test_missing_rows_in_todo_card_is_an_error(self):
        out = _card_dsl.render_template("todo-card", values_json='{"title":"T"}')
        self.assertFalse(out["ok"])
        self.assertIn("rows", out["error"])

    def test_all_keys_present_still_compiles(self):
        out = _card_dsl.render_template(
            "review-card",
            values_json='{"owner_name":"a","title":"b","delivered_at":"c","record_id":"r","selected_score":0}',
        )
        self.assertTrue(out["ok"], out.get("error"))


class TestScoreRounds(unittest.TestCase):
    def test_rounds_attribute_limits_handler_depth(self):
        xml = "<card title='t'><score rounds='3' bind-record='r'/></card>"
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertIn("review_score_r0", out["handlers"])
        self.assertIn("review_score_r2", out["handlers"])
        self.assertNotIn("review_score_r3", out["handlers"], "rounds=3 must not pre-register r3")

    def test_invalid_rounds_falls_back_to_default(self):
        for bad in ("0", "-2", "abc", ""):
            xml = f"<card title='t'><score rounds='{bad}' bind-record='r'/></card>"
            out = _card_dsl.render_card(xml)
            self.assertTrue(out["ok"], f"rounds={bad!r} should fall back, got {out.get('error')}")
            self.assertIn("review_score_r19", out["handlers"], f"rounds={bad!r} → default 20")

    def test_default_review_template_still_20_rounds(self):
        out = _card_dsl.render_card(REVIEW_XML)
        self.assertTrue(out["ok"], out.get("error"))
        self.assertIn("review_score_r19", out["handlers"])
        self.assertNotIn("review_score_r20", out["handlers"])


class TestSameActionComments(unittest.TestCase):
    def test_second_comment_action_id_is_distinct(self):
        xml = "<card title='t'><comment bind-record='r'/><comment bind-record='r'/></card>"
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"], out.get("error"))
        comments = _interactive(out, "input")
        self.assertEqual(len(comments), 2)
        v0, v1 = comments[0][1], comments[1][1]
        self.assertEqual(v0["action_id"], "review_input_r0")
        self.assertEqual(v1["action_id"], "review_input_1_r0")
        self.assertNotEqual(v0["action_id"], v1["action_id"])
        # input names stay unique too (pre-existing guarantee)
        self.assertNotEqual(comments[0][0]["name"], comments[1][0]["name"])


if __name__ == "__main__":
    unittest.main()
