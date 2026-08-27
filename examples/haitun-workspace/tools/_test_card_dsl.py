"""Standalone unit tests for the card DSL engine (_card_dsl).

Runs without the full psi-agent runtime: the two runtime deps
(_todo_card_impl, _runtime_paths) are stubbed so the 2.0-card compile path,
validation, the Action six-ring behaviour, template filling and XML escaping
can all be exercised in isolation.

Run:  PYTHONPATH=. python -m pytest test_card_dsl_standalone.py -q
  or: PYTHONPATH=. python test_card_dsl_standalone.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import types
import unittest
import xml.etree.ElementTree as ET

try:
    import xmlschema
except ImportError:
    xmlschema = None

# ── stub runtime deps that _card_dsl imports at module load ──────────────────
_stub_impl = types.ModuleType("_todo_card_impl")
_stub_impl._UNDO_ROUNDS = 20
_stub_impl._build_card_from_state = lambda state: {"_state": state, "_legacy_state": state, "schema": "2.0"}
_stub_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_stub_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _stub_impl)

_stub_paths = types.ModuleType("_runtime_paths")
_stub_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules.setdefault("_runtime_paths", _stub_paths)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl  # noqa: E402


def _score_value(card, block_index=1):
    """First score button's callback value."""
    col = card["body"]["elements"][block_index]["columns"][0]
    return col["elements"][0]["behaviors"][0]["value"]


REVIEW_XML = (
    '<card title="TODO 评价" template="blue">'
    '<info label="执行人" value="黄子建"/>'
    '<score min="1" max="5" bind-record="recX" selected="4"/>'
    '<comment placeholder="写点评语" bind-record="recX"/>'
    '<action-row><button text="打回重做" type="reject" action="review_reject"/></action-row>'
    "</card>"
)


class TestValidation(unittest.TestCase):
    def test_missing_title(self):
        out = _card_dsl.render_card('<card><info label="a" value="b"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("title", out["error"])

    def test_wrong_root(self):
        out = _card_dsl.render_card('<info label="a" value="b"/>')
        self.assertFalse(out["ok"])
        self.assertIn("root element must be <card>", out["error"])

    def test_bad_xml(self):
        out = _card_dsl.render_card('<card title="x"><info></card>')
        self.assertFalse(out["ok"])
        self.assertIn("not valid XML", out["error"])

    def test_unknown_template(self):
        out = _card_dsl.render_card('<card title="x" template="purple"/>')
        self.assertFalse(out["ok"])
        self.assertIn("template", out["error"])

    def test_unknown_element(self):
        out = _card_dsl.render_card('<card title="x"><foo/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("unknown element", out["error"])

    def test_unknown_button_type(self):
        out = _card_dsl.render_card(
            '<card title="x"><action-row><button text="a" type="rainbow" action="review_reject"/></action-row></card>'
        )
        self.assertFalse(out["ok"])
        self.assertIn("type", out["error"])

    def test_button_without_action(self):
        out = _card_dsl.render_card('<card title="x"><action-row><button text="a"/></action-row></card>')
        self.assertFalse(out["ok"])

    def test_unregistered_action_fails_closed(self):
        out = _card_dsl.render_card(
            '<card title="x"><action-row><button text="a" action="mystery"/></action-row></card>'
        )
        self.assertFalse(out["ok"])
        self.assertIn("handler", out["error"])

    def test_info_requires_label_value(self):
        out = _card_dsl.render_card('<card title="x"><info label="only"/></card>')
        self.assertFalse(out["ok"])


class TestCompile(unittest.TestCase):
    def test_basic_shape(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json='{"owner_name":"黄子建"}')
        self.assertTrue(out["ok"], out.get("error"))
        card = out["card"]
        self.assertEqual(card["schema"], "2.0")
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "TODO 评价")
        self.assertEqual(len(card["body"]["elements"]), 4)

    def test_template_color_mapping(self):
        for tmpl in ("blue", "green", "red", "grey"):
            out = _card_dsl.render_card(f'<card title="x" template="{tmpl}"/>')
            self.assertTrue(out["ok"])
            self.assertEqual(out["card"]["header"]["template"], tmpl)

    def test_button_semantic_color(self):
        cases = {
            "accept": "primary",
            "reject": "danger",
            "danger": "danger",
            "default": "default",
            "primary": "primary",
        }
        for sem, feishu in cases.items():
            out = _card_dsl.render_card(
                f'<card title="x"><action-row>'
                f'<button text="a" type="{sem}" action="review_reject"/></action-row></card>'
            )
            self.assertTrue(out["ok"], out.get("error"))
            btn = out["card"]["body"]["elements"][0]["columns"][0]["elements"][0]
            self.assertEqual(btn["type"], feishu, f"{sem} should map to {feishu}")

    def test_score_selected_highlight(self):
        out = _card_dsl.render_card(REVIEW_XML)
        cols = out["card"]["body"]["elements"][1]["columns"]
        # selected="4" → the 4th score button is primary + ✓
        selected = [c["elements"][0] for c in cols if c["elements"][0]["type"] == "primary"]
        self.assertEqual(len(selected), 1)
        self.assertIn("✓", selected[0]["text"]["content"])
        self.assertIn("4", selected[0]["text"]["content"])

    def test_comment_has_confirm(self):
        out = _card_dsl.render_card(REVIEW_XML)
        inputs = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "input"]
        self.assertEqual(len(inputs), 1)
        self.assertIn("confirm", inputs[0])

    def test_comment_prefill_on_rebuild(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json='{"comment_value":"上次评语"}')
        inputs = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "input"]
        self.assertEqual(inputs[0]["value"], "上次评语")


class TestActionSixRing(unittest.TestCase):
    def test_round_naming_and_bump(self):
        out0 = _card_dsl.render_card(REVIEW_XML, round_=0)
        out1 = _card_dsl.render_card(REVIEW_XML, round_=1)
        self.assertEqual(_score_value(out0["card"])["action"], "review_score_r0")
        self.assertEqual(_score_value(out1["card"])["action"], "review_score_r1")

    def test_handlers_preregister_all_rounds(self):
        out = _card_dsl.render_card(REVIEW_XML)
        h = out["handlers"]
        # 3 actions (score/comment/reject) x 20 rounds
        self.assertEqual(len(h), 60)
        self.assertEqual(h["review_score_r0"], "feishu_review_card_select")
        self.assertEqual(h["review_score_r19"], "feishu_review_card_select")
        self.assertEqual(h["review_input_r5"], "feishu_review_input")
        self.assertEqual(h["review_reject_r0"], "feishu_review_reject")

    def test_round_clamped(self):
        out = _card_dsl.render_card(REVIEW_XML, round_=999)
        # clamped to _MAX_ROUNDS-1 = 19
        self.assertEqual(_score_value(out["card"])["action"], "review_score_r19")

    def test_round_negative_or_bad(self):
        for bad in (-5, "abc", None):
            out = _card_dsl.render_card(REVIEW_XML, round_=bad)
            self.assertTrue(out["ok"])
            self.assertEqual(_score_value(out["card"])["action"], "review_score_r0")

    def test_value_carries_bind_record_action_round_score(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json='{"owner_name":"黄子建"}', round_=2)
        v = _score_value(out["card"])
        self.assertEqual(v["record_id"], "recX")          # bind-record wins
        self.assertEqual(v["owner_name"], "黄子建")         # context injected
        self.assertEqual(v["action"], "review_score_r2")
        self.assertEqual(v["round"], 2)
        self.assertEqual(v["score"], 1)                    # first button = score 1
        self.assertEqual(v["action_id"], "review_score_1_r2")

    def test_bind_record_overrides_context_record_id(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json='{"record_id":"ctxRec"}')
        self.assertEqual(_score_value(out["card"])["record_id"], "recX")

    def test_custom_action_via_overrides(self):
        xml = '<card title="x"><action-row><button text="归档" action="archive_it"/></action-row></card>'
        out = _card_dsl.render_card(xml, handler_overrides_json='{"archive_it":"my_archive_tool"}')
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["handlers"]["archive_it_r0"], "my_archive_tool")

    def test_score_action_override(self):
        xml = '<card title="x"><score min="1" max="3" action="grade" bind-record="r"/></card>'
        out = _card_dsl.render_card(xml, handler_overrides_json='{"grade":"grade_tool"}')
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(_score_value(out["card"], 0)["action"], "grade_r0")


class TestBadInputs(unittest.TestCase):
    def test_bad_context_json(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json="{not json")
        self.assertFalse(out["ok"])
        self.assertIn("context_json", out["error"])

    def test_context_not_object(self):
        out = _card_dsl.render_card(REVIEW_XML, context_json="[1,2]")
        self.assertFalse(out["ok"])

    def test_bad_overrides_json(self):
        out = _card_dsl.render_card(REVIEW_XML, handler_overrides_json="nope")
        self.assertFalse(out["ok"])

    def test_empty_xml(self):
        out = _card_dsl.render_card("")
        self.assertFalse(out["ok"])


class TestTemplateAndEscaping(unittest.TestCase):
    def test_xml_escape_in_fill(self):
        xml = "<card title=\"{t}\"/>"
        filled = _card_dsl._fill_template(xml, {"t": '<b>&"x'})
        self.assertNotIn("<b>", filled)
        self.assertIn("&lt;b&gt;", filled)

    def test_review_template_renders(self):
        vals = json.dumps(
            {
                "owner_name": "黄子建",
                "title": "优化方案",
                "delivered_at": "08-27",
                "record_id": "recABC",
                "selected_score": 3,
            },
            ensure_ascii=False,
        )
        out = _card_dsl.render_template("review-card", values_json=vals, context_json='{"owner_name":"黄子建"}')
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["title"]["content"], "TODO 评价")
        # selected_score=3 highlights the 3rd button
        cols = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")["columns"]
        hl = [c["elements"][0] for c in cols if c["elements"][0]["type"] == "primary"]
        self.assertTrue(any("3" in b["text"]["content"] for b in hl))

    def test_template_note_optional_line(self):
        # note empty → no status line; note set → an info line appears
        base = '{"owner_name":"a","title":"b","delivered_at":"c","record_id":"r","selected_score":0}'
        out_empty = _card_dsl.render_template("review-card", values_json=base)
        n_empty = len(out_empty["card"]["body"]["elements"])
        d = json.loads(base)
        d["note"] = "已打回重做"
        out_note = _card_dsl.render_template("review-card", values_json=json.dumps(d, ensure_ascii=False))
        n_note = len(out_note["card"]["body"]["elements"])
        self.assertEqual(n_note, n_empty + 1)

    def test_template_path_traversal_blocked(self):
        for bad in ("../secret", "a/b", "..\\x"):
            out = _card_dsl.render_template(bad)
            self.assertFalse(out["ok"])
            self.assertIn("invalid template_name", out["error"])

    def test_template_not_found(self):
        out = _card_dsl.render_template("no-such-card")
        self.assertFalse(out["ok"])
        self.assertIn("not found", out["error"])


class TestListCard(unittest.TestCase):
    def test_list_card_uses_legacy_state(self):
        xml = (
            '<card title="今日 TODO"><list>'
            '<row title="写方案" task-guid="g1"/><row title="评审" done="true"/></list></card>'
        )
        out = _card_dsl.render_card(xml, context_json='{"ledger_app_token":"tok","ledger_table_id":"tbl"}')
        self.assertTrue(out["ok"], out.get("error"))
        state = out["card"]["_state"]  # from stub _build_card_from_state
        self.assertEqual(state["title"], "今日 TODO")
        self.assertEqual(len(state["rows"]), 2)
        self.assertTrue(state["rows"][1]["locked"])  # done row is read-only
        # only the not-done row (index 0) gets tick/untick handlers
        self.assertIn("todo_tick_0_r0", out["handlers"])
        self.assertNotIn("todo_tick_1_r0", out["handlers"])

    def test_list_mixed_with_2_0_rejected(self):
        xml = '<card title="x"><list><row title="a"/></list><info label="l" value="v"/></card>'
        out = _card_dsl.render_card(xml)
        self.assertFalse(out["ok"])
        self.assertIn("list", out["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestEdgeFindings(unittest.TestCase):
    """Boundary probes. Tests named *_BUG_* document defects (current behaviour
    asserted so they pass, with the correct behaviour noted in comments)."""

    def test_score_max_capped_at_9(self):
        out = _card_dsl.render_card('<card title="x"><score min="1" max="100" bind-record="r"/></card>')
        cols = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")["columns"]
        self.assertEqual(len(cols), 9)  # hi clamped to 9

    def test_score_min_gt_max_errors(self):
        # Fixed: min>max is a template bug — was silently collapsed to a single
        # button ("5 to 2" looked like "only 5"), now raised so the author sees it.
        out = _card_dsl.render_card('<card title="x"><score min="5" max="2" bind-record="r"/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("min", out.get("error", ""))
        self.assertIn("max", out.get("error", ""))

    def test_selected_out_of_range_highlights_nothing(self):
        out = _card_dsl.render_card('<card title="x"><score min="1" max="5" selected="7" bind-record="r"/></card>')
        cols = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")["columns"]
        hl = [c for c in cols if c["elements"][0]["type"] == "primary"]
        self.assertEqual(hl, [])

    def test_placeholder_no_reexpansion(self):
        # Fixed: single-pass substitution. A value that literally contains another
        # {key} must NOT be re-expanded — each placeholder resolves once.
        filled = _card_dsl._fill_template(
            '<card title="x"><info label="a" value="{owner}"/><info label="b" value="{evil}"/></card>',
            {"owner": "{evil}", "evil": "HACKED"},
        )
        # owner keeps its literal text (XML-escaped braces stay literal), evil is HACKED
        self.assertIn('value="{evil}"', filled)  # owner NOT re-expanded
        self.assertIn('label="b" value="HACKED"', filled)

    def test_xml_structure_injection_is_escaped(self):
        # The important guard: a value can NOT inject new XML elements.
        filled = _card_dsl._fill_template(
            '<card title="x"><info label="a" value="{v}"/></card>',
            {"v": '"/><button action="review_reject" text="pwn"/>'},
        )
        self.assertNotIn("<button", filled)
        self.assertIn("&lt;button", filled)

    def test_unfilled_placeholder_leaks_to_card(self):
        # A missing key leaves the literal {key} visible on the card instead of
        # erroring or blanking. Correct behaviour: blank it or fail loudly.
        out = _card_dsl.render_card('<card title="T"><info label="a" value="{missing}"/></card>')
        self.assertTrue(out["ok"])
        self.assertIn("{missing}", out["card"]["body"]["elements"][0]["content"])

    def test_comment_input_names_unique(self):
        # Fixed: element index is folded into the input name, so two comments
        # sharing a bind-record no longer collide.
        out = _card_dsl.render_card('<card title="x"><comment bind-record="r"/><comment bind-record="r"/></card>')
        names = [e["name"] for e in out["card"]["body"]["elements"] if e.get("tag") == "input"]
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])  # no collision

    def test_score_without_bind_record_has_no_target(self):
        out = _card_dsl.render_card('<card title="x"><score min="1" max="2"/></card>')
        cs = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")
        v = cs["columns"][0]["elements"][0]["behaviors"][0]["value"]
        self.assertNotIn("record_id", v)  # tool receives no ledger target

    def test_builtin_handler_wins_over_override(self):
        out = _card_dsl.render_card(
            '<card title="x"><score min="1" max="2" bind-record="r"/></card>',
            handler_overrides_json='{"review_score":"MY_TOOL"}',
        )
        self.assertEqual(out["handlers"]["review_score_r0"], "feishu_review_card_select")

    def test_empty_card_compiles_empty_body(self):
        out = _card_dsl.render_card('<card title="empty"/>')
        self.assertTrue(out["ok"])
        self.assertEqual(out["card"]["body"]["elements"], [])

    def test_parse_round_truncates_float_string_to_zero(self):
        # int("5.5") raises → falls to 0. int(5.5) truncates → 5.
        self.assertEqual(_card_dsl._parse_round("5.5"), 0)
        self.assertEqual(_card_dsl._parse_round(5.9), 5)


class TestErrorMessageQuality(unittest.TestCase):
    """Errors must point at the real mistake, not a downstream symptom."""

    def test_missing_action_says_missing_action_not_handler(self):
        out = _card_dsl.render_card('<card title="x"><action-row><button text="a"/></action-row></card>')
        self.assertFalse(out["ok"])
        self.assertIn("requires an action", out["error"])
        self.assertNotIn("no handler", out["error"])  # not the confusing downstream msg

    def test_empty_action_row_errors(self):
        out = _card_dsl.render_card('<card title="x"><action-row/></card>')
        self.assertFalse(out["ok"])
        self.assertIn("at least one <button>", out["error"])

    def test_unknown_element_message_lists_list(self):
        out = _card_dsl.render_card('<card title="x"><widget/></card>')
        self.assertIn("list", out["error"])  # vocabulary hint now includes list

    def test_bad_type_before_handler_lookup(self):
        # A bad type on a button with an otherwise-unregistered action should
        # report the type problem (attribute-level), not the handler.
        out = _card_dsl.render_card(
            '<card title="x"><action-row><button text="a" type="neon" action="zzz"/></action-row></card>'
        )
        self.assertIn("type", out["error"])


@unittest.skipUnless(xmlschema is not None, "xmlschema not installed")
class TestXSDvsEngine(unittest.TestCase):
    """The XSD (fed to the LLM) and the runtime validator must agree on
    accept/reject, or the spec the model follows drifts from what runs."""

    @classmethod
    def setUpClass(cls):
        xsd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "card.xsd")
        cls.schema = xmlschema.XMLSchema(xsd)

    def _engine_ok(self, xml):
        # give a handler for arbitrary actions so only structural validity matters
        return _card_dsl.render_card(xml, handler_overrides_json='{"a":"t","act":"t"}')["ok"]

    def _both(self, xml):
        return self.schema.is_valid(xml), self._engine_ok(xml)

    def test_agreement_matrix(self):
        cases = [
            '<card title="t"><info label="a" value="b"/></card>',
            '<card title="t"><score min="1" max="5" bind-record="r"/></card>',
            '<card title="t"><comment bind-record="r"/></card>',
            '<card title="t"><action-row><button text="x" type="reject" action="act"/></action-row></card>',
            '<card title="t"><list><row title="a"/></list></card>',
            '<card title="t" template="purple"/>',
            '<card><info label="a" value="b"/></card>',
            '<card title="t"><action-row><button text="x" type="neon" action="a"/></action-row></card>',
            '<card title="t"><action-row><button text="x"/></action-row></card>',
            '<card title="t"><info label="a"/></card>',
            '<card title="t"><widget/></card>',
            '<card title="t"><action-row/></card>',
        ]
        diverged = []
        for xml in cases:
            xsd_ok, eng_ok = self._both(xml)
            if xsd_ok != eng_ok:
                diverged.append((xml, xsd_ok, eng_ok))
        self.assertEqual(diverged, [], f"XSD/engine disagree on: {diverged}")

    def test_skill_example_and_templates_valid(self):
        tdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "templates")

        def _read(name):
            with open(os.path.join(tdir, name), encoding="utf-8") as fh:
                return fh.read()

        # filled templates must pass the XSD (raw templates hold {rows}/{note} placeholders)
        review = _card_dsl._fill_template(
            _read("review-card.xml"),
            {
                "owner_name": "黄子建",
                "title": "T",
                "delivered_at": "d",
                "record_id": "r",
                "selected_score": 3,
                "note": "",
            },
        )
        self.assertTrue(self.schema.is_valid(review), list(self.schema.iter_errors(review)))
        todo = _card_dsl._fill_template(
            _read("todo-card.xml"),
            {"title": "今日 TODO", "rows": [{"title": "a", "task_guid": "g", "bind_record": "r"}]},
        )
        self.assertTrue(self.schema.is_valid(todo), list(self.schema.iter_errors(todo)))


class TestFuzzNeverCrashes(unittest.TestCase):
    """render_card must never raise for caller-side input — always {ok:bool}."""

    def test_random_bytes_as_xml(self):
        random.seed(42)
        alphabet = '<>/"=card infoscb .{}&;\n\t黄'
        for _ in range(500):
            n = random.randint(0, 40)
            junk = "".join(random.choice(alphabet) for _ in range(n))
            try:
                out = _card_dsl.render_card(junk)
            except Exception as e:
                self.fail(f"render_card raised {e!r} on input {junk!r}")
            self.assertIn("ok", out)
            self.assertIsInstance(out["ok"], bool)

    def test_non_string_inputs(self):
        for bad in (None, 123, [], {}, b"<card/>"):
            out = _card_dsl.render_card(bad)  # type: ignore[arg-type]
            self.assertFalse(out["ok"])

    def test_deeply_nested_does_not_crash(self):
        xml = '<card title="t">' + "<action-row>" * 50 + "</action-row>" * 50 + "</card>"
        out = _card_dsl.render_card(xml)
        self.assertIn("ok", out)

    def test_huge_score_range(self):
        out = _card_dsl.render_card('<card title="t"><score min="1" max="999999" bind-record="r"/></card>')
        self.assertTrue(out["ok"])
        cols = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")["columns"]
        self.assertLessEqual(len(cols), 9)


class TestEscapingTorture(unittest.TestCase):
    def test_special_chars_in_info_do_not_break_json(self):
        xml = '<card title="t"><info label="a" value="&lt;b&gt;&amp;&quot;x"/></card>'
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"], out.get("error"))
        # the compiled card must round-trip through json cleanly
        json.dumps(out["card"])

    def test_emoji_and_cjk_preserved(self):
        xml = '<card title="季度评价 🎯"><info label="负责人" value="黄子建 ✅"/></card>'
        out = _card_dsl.render_card(xml)
        self.assertTrue(out["ok"])
        self.assertIn("🎯", out["card"]["header"]["title"]["content"])
        self.assertIn("✅", out["card"]["body"]["elements"][0]["content"])

    def test_template_value_with_all_xml_metachars(self):
        # All five XML metacharacters — including quotes — must survive because
        # every fill target is a double-quoted attribute.
        filled = _card_dsl._fill_template('<card title="{t}"/>', {"t": '<>&"\''})
        root = ET.fromstring(filled)  # must parse
        self.assertEqual(root.get("title"), '<>&"\'')

    def test_double_quote_in_value_does_not_break_card(self):
        # A realistic title/name with a double quote must still compile.
        out = _card_dsl.render_template(
            "review-card",
            values_json='{"owner_name":"He said \\"hi\\"","title":"季度 \\"优秀\\" 评价",'
            '"delivered_at":"d","record_id":"r","selected_score":0}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        infos = [e["content"] for e in out["card"]["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("优秀" in i for i in infos))

    def test_curly_braces_in_value_not_treated_as_placeholder(self):
        # A literal value containing {x} where x is NOT a provided key stays literal.
        filled = _card_dsl._fill_template('<card title="{title}"/>', {"title": "a{b}c"})
        self.assertEqual(filled, '<card title="a{b}c"/>')


class TestRebuildSemantics(unittest.TestCase):
    """The score-click / comment / reject rebuild loop is the heart of the
    'real card matches original' acceptance criterion."""

    XML = (
        '<card title="t"><score min="1" max="5" bind-record="r"/>'
        '<comment bind-record="r"/>'
        '<action-row><button text="打回" type="reject" action="review_reject"/></action-row></card>'
    )

    def _all_action_names(self, card):
        names = set()

        def walk(o):
            if isinstance(o, dict):
                if o.get("type") == "callback":
                    v = o.get("value", {})
                    if isinstance(v, dict) and "action" in v:
                        names.add(v["action"])
                for x in o.values():
                    walk(x)
            elif isinstance(o, list):
                for x in o:
                    walk(x)

        walk(card)
        return names

    def test_every_rendered_action_has_a_handler(self):
        # Any action name that appears in a callback value MUST be a key in handlers,
        # or the click would dead-end.
        for rnd in range(5):
            out = _card_dsl.render_card(self.XML, round_=rnd)
            self.assertTrue(out["ok"], out.get("error"))
            action_names = self._all_action_names(out["card"])
            for name in action_names:
                self.assertIn(name, out["handlers"], f"round {rnd}: {name} has no handler")

    def test_round_bump_changes_all_action_names(self):
        a0 = self._all_action_names(_card_dsl.render_card(self.XML, round_=0)["card"])
        a1 = self._all_action_names(_card_dsl.render_card(self.XML, round_=1)["card"])
        # single-use: no action name from round 0 may reappear in round 1
        self.assertEqual(a0 & a1, set(), f"round names collide across rebuild: {a0 & a1}")

    def test_handlers_stable_across_rounds(self):
        # The handler MAP (all 20 rounds pre-registered) is identical regardless of
        # the current round — rebuilds never need to re-register.
        h0 = _card_dsl.render_card(self.XML, round_=0)["handlers"]
        h5 = _card_dsl.render_card(self.XML, round_=5)["handlers"]
        self.assertEqual(h0, h5)

    def test_selected_score_survives_rebuild(self):
        out = _card_dsl.render_card(
            '<card title="t"><score min="1" max="5" selected="3" bind-record="r"/></card>', round_=2
        )
        cols = next(e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set")["columns"]
        hi = [c["elements"][0] for c in cols if c["elements"][0]["type"] == "primary"]
        self.assertEqual(len(hi), 1)
        self.assertIn("3", hi[0]["text"]["content"])
