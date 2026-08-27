"""Spec-conformance suite: every assertion maps to a written promise in the
design docs (《todo:交互界面通用元素-以卡片为例》 + 《通用元素-卡片第一版》 +
Dustin's four review points).

Each test name cites its source (spec section or doc claim). A failure here means
the engine no longer honours a documented promise — the strictest acceptance bar
short of clicking a real Feishu card. Run:

    PYTHONPATH=. python -m pytest _test_card_dsl_spec.py -q -o addopts=""
"""

from __future__ import annotations

import inspect
import json
import os
import sys
import types
import xml.etree.ElementTree as ET
from typing import Any

# ── stub the two runtime deps the engine imports at load ─────────────────────
_impl = types.ModuleType("_todo_card_impl")
_impl._UNDO_ROUNDS = 20
_impl._build_card_from_state = lambda state: {"schema": "2.0", "_legacy_state": state, "_state": state}
_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _impl)

_paths = types.ModuleType("_runtime_paths")
_paths.agent_dir = lambda: os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.modules.setdefault("_runtime_paths", _paths)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _card_dsl  # noqa: E402


def _elements(card: dict[str, Any]) -> list[dict[str, Any]]:
    return card["body"]["elements"]


def _walk_values(card: dict[str, Any]):
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


class TestThreeTierModel:
    """§3.1 三层结构:元素→属性→映射;颜色绝不出现在 XML,只由引擎映射表决定。"""

    def test_color_never_in_xml_only_semantics(self):
        # Business writes type="reject" (semantics); engine emits type="danger" (color).
        out = _card_dsl.render_card(
            '<card title="t"><action-row><button text="打回" type="reject" action="review_reject"/></action-row></card>'
        )
        assert out["ok"]
        btn = _elements(out["card"])[0]["columns"][0]["elements"][0]
        assert btn["type"] == "danger"  # mapped, not the literal "reject"

    def test_full_button_semantic_mapping_table(self):
        # §3.1 mapping table, verbatim: accept→primary, reject→danger, danger→danger,
        # default→default, primary→primary.
        expected = {
            "accept": "primary",  # 绿是设计意图,飞书无绿→落蓝
            "reject": "danger",
            "danger": "danger",
            "default": "default",
            "primary": "primary",
        }
        for sem, color in expected.items():
            out = _card_dsl.render_card(
                f'<card title="t"><action-row>'
                f'<button text="x" type="{sem}" action="review_reject"/></action-row></card>'
            )
            assert out["ok"], out.get("error")
            assert _elements(out["card"])[0]["columns"][0]["elements"][0]["type"] == color

    def test_full_header_template_mapping_table(self):
        # §3.1: card template → header.template, native colors, full mapping.
        for tmpl in ("blue", "green", "red", "grey"):
            out = _card_dsl.render_card(f'<card title="t" template="{tmpl}"/>')
            assert out["ok"]
            assert out["card"]["header"]["template"] == tmpl

    def test_reject_maps_to_red_accept_to_blue_not_green(self):
        # §3.1 飞书限制:accept 的绿色做不出,先落蓝(primary).
        acc = _card_dsl.render_card(
            '<card title="t"><action-row><button text="通过" type="accept" action="review_reject"/></action-row></card>'
        )
        assert _elements(acc["card"])[0]["columns"][0]["elements"][0]["type"] == "primary"


class TestCompileDuties:
    """§3.2.1 编译职责:XML→飞书 2.0 JSON;自动颜色映射;input 自动附加 confirm。"""

    def test_output_is_feishu_card_2_0(self):
        out = _card_dsl.render_card('<card title="t"/>')
        assert out["ok"]
        assert out["card"]["schema"] == "2.0"
        assert "header" in out["card"] and "body" in out["card"]

    def test_no_feishu_concepts_leak_into_required_xml(self):
        # Business XML uses only DSL words; a Feishu-JSON word like "behaviors"
        # as an element must be rejected.
        out = _card_dsl.render_card('<card title="t"><behaviors/></card>')
        assert not out["ok"]

    def test_input_auto_gets_confirm(self):
        # §3.2.1 + §3.3: input 值仅 confirm 可回传 → 引擎自动加 confirm.
        out = _card_dsl.render_card('<card title="t"><comment bind-record="r"/></card>')
        inp = next(e for e in _elements(out["card"]) if e.get("tag") == "input")
        assert "confirm" in inp
        assert inp["confirm"]["title"]["content"]  # non-empty confirm dialog

    def test_comment_is_input_not_unsupported_textarea(self):
        # §3.3: textarea/form_container/button_group 不支持 → 用 input 规避.
        out = _card_dsl.render_card('<card title="t"><comment bind-record="r"/></card>')
        tags = {e.get("tag") for e in _elements(out["card"])}
        assert "input" in tags
        assert "textarea" not in tags and "form_container" not in tags


REVIEW = (
    '<card title="TODO 评价" template="blue">'
    '<info label="执行人" value="黄子建"/>'
    '<score min="1" max="5" rounds="20" bind-record="recXXX" selected="4"/>'
    '<comment placeholder="写点评语" bind-record="recXXX"/>'
    '<action-row><button text="打回重做" type="reject" action="review_reject"/></action-row>'
    "</card>"
)


class TestActionSixRings:
    """§3.2.2 Action 统一处理六环 — the spec's most detailed contract."""

    # ring 1 声明: action + bind-record are the only two callback words in XML.
    def test_ring1_declaration_binds_action_and_record(self):
        out = _card_dsl.render_card(REVIEW, context_json='{"owner_name":"黄子建"}')
        values = list(_walk_values(out["card"]))
        # every callback value carries a round-scoped action name...
        assert values and all(v["action"].startswith("review_") for v in values)
        # ...and the bind-record ones surface it as record_id (score/comment bind
        # to recXXX; the reject button intentionally carries no record).
        bound = [v for v in values if "record_id" in v]
        assert bound and all(v["record_id"] == "recXXX" for v in bound)

    # ring 2 生成: {action}_r{round}, 0-based, ceiling 20.
    def test_ring2_round_naming_scheme(self):
        for rnd in (0, 3, 19):
            out = _card_dsl.render_card(REVIEW, round_=rnd)
            names = {v["action"] for v in _walk_values(out["card"])}
            assert all(n.endswith(f"_r{rnd}") for n in names), names

    def test_ring2_round_ceiling_is_20(self):
        out = _card_dsl.render_card(REVIEW, round_=99)
        names = {v["action"] for v in _walk_values(out["card"])}
        assert all(n.endswith("_r19") for n in names)  # clamped to _MAX_ROUNDS-1

    def test_ring2_round_floor_is_0(self):
        out = _card_dsl.render_card(REVIEW, round_=-5)
        names = {v["action"] for v in _walk_values(out["card"])}
        assert all(n.endswith("_r0") for n in names)

    # ring 3 映射: ALL rounds pre-registered to the right direct-dispatch tool.
    def test_ring3_all_rounds_preregistered(self):
        out = _card_dsl.render_card(REVIEW)
        h = out["handlers"]
        for r in range(20):
            assert h[f"review_score_r{r}"] == "feishu_review_card_select"
            assert h[f"review_input_r{r}"] == "feishu_review_input"
            assert h[f"review_reject_r{r}"] == "feishu_review_reject"

    def test_ring3_builtin_tool_names_match_spec(self):
        # §3.2.2 names the three tools explicitly.
        assert _card_dsl._BUILTIN_HANDLERS["review_score"] == "feishu_review_card_select"
        assert _card_dsl._BUILTIN_HANDLERS["review_input"] == "feishu_review_input"
        assert _card_dsl._BUILTIN_HANDLERS["review_reject"] == "feishu_review_reject"

    # ring 4 回调组装: value carries bind-record + context + action + round (+score).
    def test_ring4_value_contract_complete(self):
        out = _card_dsl.render_card(REVIEW, context_json='{"owner_name":"黄子建","task_guid":"g1"}', round_=2)
        score_v = next(
            v for v in _walk_values(out["card"]) if v["action"] == "review_score_r2" and "score" in v
        )
        assert score_v["record_id"] == "recXXX"      # bind-record
        assert score_v["owner_name"] == "黄子建"       # context
        assert score_v["task_guid"] == "g1"           # context
        assert score_v["action"] == "review_score_r2"  # action + round
        assert score_v["round"] == 2
        assert score_v["score"] in range(1, 6)         # current score

    def test_ring4_every_click_target_is_dispatchable(self):
        # Every action embedded in a value MUST have a handler, else the click dead-ends.
        for rnd in range(20):
            out = _card_dsl.render_card(REVIEW, round_=rnd)
            for v in _walk_values(out["card"]):
                assert v["action"] in out["handlers"], f"r{rnd}: {v['action']} unroutable"

    # ring 5 状态重建: score highlight (single-select), comment refill, round bump.
    def test_ring5_score_single_select_highlight(self):
        out = _card_dsl.render_card(REVIEW)  # selected="4"
        cols = next(e for e in _elements(out["card"]) if e.get("tag") == "column_set")["columns"]
        hi = [c["elements"][0] for c in cols if c["elements"][0]["type"] == "primary"]
        assert len(hi) == 1  # exactly one highlighted (互斥)
        assert "4" in hi[0]["text"]["content"]
        assert "✓" in hi[0]["text"]["content"]

    def test_ring5_comment_refill_on_rebuild(self):
        out = _card_dsl.render_card(REVIEW, context_json='{"comment_value":"上次的评语"}')
        inp = next(e for e in _elements(out["card"]) if e.get("tag") == "input")
        assert inp["value"] == "上次的评语"

    def test_ring5_round_bump_replaces_every_action_name(self):
        # single-use: no action name survives a rebuild (round+1).
        r0 = {v["action"] for v in _walk_values(_card_dsl.render_card(REVIEW, round_=0)["card"])}
        r1 = {v["action"] for v in _walk_values(_card_dsl.render_card(REVIEW, round_=1)["card"])}
        assert r0.isdisjoint(r1)

    # ring 6 业务边界: engine only routes + rebuilds; it must not embed business logic.
    def test_ring6_engine_emits_no_business_effect(self):
        # render is pure: no ledger token/table required to compile a card,
        # and calling twice yields identical output (no side effects).
        a = _card_dsl.render_card(REVIEW)
        b = _card_dsl.render_card(REVIEW)
        assert a == b


class TestTemplatePromises:
    """《通用元素-卡片第一版》: 模板只填数据不写结构;{key} 自动 XML 转义;{rows} 展开。"""

    def test_review_template_fill_data_only(self):
        out = _card_dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {"owner_name": "黄子建", "title": "优化方案", "delivered_at": "08-27",
                 "record_id": "recX", "selected_score": 3, "note": ""},
                ensure_ascii=False,
            ),
            context_json='{"owner_name":"黄子建"}',
        )
        assert out["ok"], out.get("error")
        assert out["card"]["header"]["title"]["content"] == "TODO 评价"

    def test_placeholder_auto_xml_escape(self):
        # A value with XML metacharacters (incl. quotes) must not break the card.
        out = _card_dsl.render_template(
            "review-card",
            values_json=json.dumps(
                {"owner_name": 'He said "hi" <b>&', "title": "T", "delivered_at": "d",
                 "record_id": "r", "selected_score": 0},
                ensure_ascii=False,
            ),
        )
        assert out["ok"], out.get("error")
        json.dumps(out["card"])  # round-trips cleanly

    def test_rows_expand_in_todo_template(self):
        out = _card_dsl.render_template(
            "todo-card",
            values_json=json.dumps(
                {"title": "今日 TODO", "rows": [
                    {"title": "写方案", "task_guid": "g1", "bind_record": "r1"},
                    {"title": "评审", "done": True},
                ]},
                ensure_ascii=False,
            ),
            context_json='{"ledger_app_token":"tok","ledger_table_id":"tbl"}',
        )
        assert out["ok"], out.get("error")
        assert len(out["card"]["_legacy_state"]["rows"]) == 2

    def test_template_path_traversal_blocked(self):
        for bad in ("../secret", "a/b", "..\\x", "/etc/passwd"):
            assert not _card_dsl.render_template(bad)["ok"]


class TestDustinFourPoints:
    """Dustin 评审四点。"""

    def test_point1_xsd_exists_and_is_valid_schema(self):
        xsd = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "skills", "card-dsl", "card.xsd")
        assert os.path.isfile(xsd)
        # parses as XML and is an xs:schema
        root = ET.parse(xsd).getroot()
        assert root.tag.endswith("schema")

    def test_point2_template_is_color_only_no_freeform_style(self):
        # template accepts only the four semantic colors; no free CSS.
        assert not _card_dsl.render_card('<card title="t" template="#FF0000"/>')["ok"]
        assert not _card_dsl.render_card('<card title="t" template="rainbow"/>')["ok"]

    def test_point3_action_logic_fully_specified(self):
        # ring 1-6 all covered by TestActionSixRings; here assert the engine
        # exposes the round ceiling constant the spec fixes at 20.
        assert _card_dsl._MAX_ROUNDS == 20

    def test_point4_engine_entry_is_pure_xml_string_db_reserve(self):
        # The render entry consumes ONLY an XML string — the card definition's
        # origin (file today, DB tomorrow) is decoupled from the engine core.
        sig = inspect.signature(_card_dsl.render_card)
        assert "card_xml" in sig.parameters
        # feeding a string built in-memory (as a DB would return) works identically
        in_memory = '<card title="from-db"><info label="src" value="db"/></card>'
        out = _card_dsl.render_card(in_memory)
        assert out["ok"] and out["card"]["header"]["title"]["content"] == "from-db"


class TestSecondCardTypeZeroChange:
    """§4 通用性验证:再取一卡型(todo/list),引擎零改动即可渲染。"""

    def test_list_card_renders_without_engine_change(self):
        out = _card_dsl.render_card(
            '<card title="今日 TODO"><list><row title="a" bind-record="r1"/>'
            '<row title="b" done="true"/></list></card>',
            context_json='{"ledger_app_token":"tok","ledger_table_id":"tbl"}',
        )
        assert out["ok"], out.get("error")
        # reuses the legacy row machinery (not a re-implementation)
        assert "_legacy_state" in out["card"]

    def test_list_done_row_is_readonly(self):
        out = _card_dsl.render_card(
            '<card title="t"><list><row title="done-one" done="true"/></list></card>',
            context_json='{}',
        )
        assert out["card"]["_legacy_state"]["rows"][0]["locked"] is True

    def test_list_not_mixed_with_2_0_elements(self):
        # SKILL.md边界: list 卡第一版不与 score/comment 混用,混用报错(不静默).
        out = _card_dsl.render_card('<card title="t"><list><row title="a"/></list><score bind-record="r"/></card>')
        assert not out["ok"]
        assert "list" in out["error"]
