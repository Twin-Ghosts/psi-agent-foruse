# ruff: noqa: RUF001  # 中文全角标点是刻意排版,非歧义字符
"""Standalone unit tests for the report-card templates and the <table>/<divider> elements.

Covers the two new fixed card types (``mentor-report-card`` / ``boss-overview-card``)
added for the TODO 报表/统计卡 deliverable: placeholder filling, leftover-placeholder
failure, table-column compilation (header + data rows, empty fallback, row cap),
divider rendering, and XML escaping of user-supplied values.

Runs without the full psi-agent runtime (same stub pattern as
``_test_card_dsl.py``).

Run:  PYTHONPATH=. python -m pytest _test_report_cards.py -q
  or: PYTHONPATH=. python _test_report_cards.py
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

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


def _mentor_values(**overrides):
    values = {
        "template": "red",
        "cycle_label": "08-28",
        "mentor_name": "赵胜迪",
        "people_summary": "填报 12 人｜请假 1 人｜未按时填报 2 人",
        "goal_summary": "大目标 4｜小目标 8｜TODO 23",
        "done_summary": "已闭环 15｜进行中 6｜逾期 2",
        "score_summary": "平均分 4.2｜5分×3｜4分×6｜3分×2",
        "trend": "61%→68%→72%（近6周期）",
        "ledger_url": "https://genuineknowledge.feishu.cn/base/tblAPP?table=tblCYC",
    }
    values.update(overrides)
    return values


def _boss_values(**overrides):
    values = {
        "template": "blue",
        "cycle_label": "08-28",
        "global_summary": "目标 12｜小目标 20｜TODO 61｜已闭环 40｜逾期 5｜请假顺延 3",
        "overdue_top": "① 张三(赵胜迪团队)逾期5天",
        "trend": "65%→70%→74%（近6周期）",
        "tree_url": "https://genuineknowledge.feishu.cn/wiki/WIKIROOT",
    }
    values.update(overrides)
    return values


def _rows(n=3):
    return [
        {"level": "目标", "title": f"任务{i}", "due": "08-26", "status": "闭环", "score": "4"}
        for i in range(n)
    ]


def _render(template, values, context=None):
    return _card_dsl.render_template(
        template,
        values_json=json.dumps(values, ensure_ascii=False),
        context_json=json.dumps(context or {}, ensure_ascii=False),
    )


class TestMentorReportCard(unittest.TestCase):
    def test_header_and_summary_lines(self):
        out = _render("mentor-report-card", _mentor_values(), {"rows": _rows()})
        self.assertTrue(out["ok"], out.get("error"))
        card = out["card"]
        self.assertEqual(card["header"]["template"], "red")
        self.assertEqual(card["header"]["title"]["content"], "TODO 报表·08-28·赵胜迪团队")
        texts = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("**人员概况**：填报 12 人" in t for t in texts))
        self.assertTrue(any("**目标数量**：大目标 4" in t for t in texts))
        self.assertTrue(any("**完成情况**：已闭环 15" in t for t in texts))
        self.assertTrue(any("**评价概况**：平均分 4.2" in t for t in texts))
        self.assertTrue(any("**趋势**：61%→68%→72%" in t for t in texts))
        self.assertTrue(any("打开本周期台账" in t and "tblAPP" in t for t in texts))

    def test_template_color_placeholder(self):
        out = _render("mentor-report-card", _mentor_values(template="green"), {"rows": []})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["header"]["template"], "green")

    def test_table_compiles_header_and_rows(self):
        out = _render("mentor-report-card", _mentor_values(), {"rows": _rows(3)})
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(
            [c["display_name"] for c in table["columns"]],
            ["层级", "标题", "截止", "状态", "打分"],
        )
        self.assertEqual(len(table["rows"]), 3)
        self.assertEqual(table["rows"][0]["c0"], "目标")
        self.assertEqual(table["rows"][0]["c1"], "任务0")
        self.assertEqual(table["rows"][0]["c3"], "闭环")

    def test_table_empty_fallback(self):
        out = _render("mentor-report-card", _mentor_values(template="blue"), {"rows": []})
        self.assertTrue(out["ok"], out.get("error"))
        sets = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "column_set"]
        self.assertEqual(sets, [])
        self.assertTrue(
            any("本周期暂无明细" in e["content"] for e in out["card"]["body"]["elements"]
                if e.get("tag") == "markdown")
        )

    def test_table_row_cap(self):
        out = _render("mentor-report-card", _mentor_values(), {"rows": _rows(15)})
        self.assertTrue(out["ok"], out.get("error"))
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        # at most 10 data rows
        self.assertEqual(len(tables[0]["rows"]), 10)

    def test_table_truncation_note(self):
        # 超过 max_rows 时表格末尾追加一行截断提示,{n} 替换为总行数。
        out = _render("mentor-report-card", _mentor_values(), {"rows": _rows(15)})
        self.assertTrue(out["ok"], out.get("error"))
        notes = [
            e["content"]
            for e in out["card"]["body"]["elements"]
            if e.get("tag") == "markdown" and "共 15 行" in e.get("content", "")
        ]
        self.assertEqual(len(notes), 1)
        # 未超过上限时不出现提示行。
        out2 = _render("mentor-report-card", _mentor_values(), {"rows": _rows(10)})
        self.assertTrue(out2["ok"], out2.get("error"))
        notes2 = [
            e for e in out2["card"]["body"]["elements"]
            if e.get("tag") == "markdown" and "共 " in e.get("content", "")
        ]
        self.assertEqual(notes2, [])

    def test_table_max_rows_override(self):
        xml = """
        <card title="t" template="blue">
          <table source="rows" max_rows="2" more="仅显示前 2 条,共 {n} 条">
            <col field="a" label="A"/>
          </table>
        </card>
        """
        out = _card_dsl.render_card(
            xml,
            context_json=json.dumps({"rows": [{"a": "1"}, {"a": "2"}, {"a": "3"}]}),
        )
        self.assertTrue(out["ok"], out.get("error"))
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["rows"]), 2)
        note = [e["content"] for e in out["card"]["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("仅显示前 2 条,共 3 条" in c for c in note))

    def test_table_bad_max_rows_fails(self):
        xml = '<card title="t"><table max_rows="abc"><col field="a"/></table></card>'
        out = _card_dsl.render_card(xml, context_json='{"rows": [{"a": "1"}]}')
        self.assertFalse(out["ok"])
        self.assertIn("max_rows", out["error"])

    def test_table_non_dict_row_filtered(self):
        # 融合后语义:非对象行自动过滤(原生 table 只渲染对象行),不报错不丢 dict 行。
        out = _render("mentor-report-card", _mentor_values(), {"rows": [{"level": "目标"}, "bad"]})
        self.assertTrue(out["ok"], out.get("error"))
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual(len(tables[0]["rows"]), 1)

    def test_divider_present(self):
        out = _render("mentor-report-card", _mentor_values(), {"rows": _rows(1)})
        hrs = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "hr"]
        self.assertEqual(len(hrs), 2)

    def test_values_are_xml_escaped(self):
        out = _render(
            "mentor-report-card",
            _mentor_values(mentor_name='张"三" <研发>', done_summary="已闭环 1 & 1"),
            {"rows": []},
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertIn('张"三" <研发>', out["card"]["header"]["title"]["content"])


class TestBossOverviewCard(unittest.TestCase):
    def test_header_and_summary_lines(self):
        out = _render("boss-overview-card", _boss_values(), {"teams": []})
        self.assertTrue(out["ok"], out.get("error"))
        card = out["card"]
        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "全公司 TODO 总览·08-28")
        texts = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("**全局指标**：目标 12" in t for t in texts))
        self.assertTrue(any("**逾期 TOP**：① 张三" in t for t in texts))
        self.assertTrue(any("打开公司工作树" in t and "WIKIROOT" in t for t in texts))

    def test_teams_table(self):
        teams = [
            {"mentor": "赵胜迪", "people": "12", "todo": "23", "closed": "15", "overdue": "2", "avg_score": "4.2"},
            {"mentor": "黄子建", "people": "8", "todo": "18", "closed": "12", "overdue": "3", "avg_score": "3.8"},
        ]
        out = _render("boss-overview-card", _boss_values(), {"teams": teams})
        self.assertTrue(out["ok"], out.get("error"))
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)  # 原生 table 一张
        table = tables[0]
        self.assertEqual(
            [c["display_name"] for c in table["columns"]],
            ["团队", "人数", "TODO", "已闭环", "逾期", "均分"],
        )
        self.assertEqual(len(table["rows"]), 2)
        self.assertEqual(table["rows"][0]["c0"], "赵胜迪")

    def test_teams_empty_fallback(self):
        out = _render("boss-overview-card", _boss_values(), {"teams": []})
        self.assertTrue(out["ok"], out.get("error"))
        self.assertTrue(
            any("暂无团队数据" in e["content"]
                for e in out["card"]["body"]["elements"] if e.get("tag") == "markdown")
        )


class TestTableElementValidation(unittest.TestCase):
    def test_missing_cols_uses_first_row_fields(self):
        # 未声明 col 时取首行全部字段作列(融合后的原生 table 语义)。
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows"/></card>',
            context_json='{"rows": [{"a": "1", "b": "2"}]}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        tables = [e for e in out["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        self.assertEqual([c["display_name"] for c in tables[0]["columns"]], ["a", "b"])
        self.assertEqual(tables[0]["rows"][0], {"c0": "1", "c1": "2"})

    def test_col_without_field_fails(self):
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows"><col label="x"/></table></card>',
            context_json='{"rows": []}',
        )
        self.assertFalse(out["ok"])
        self.assertIn("field", out["error"])

    def test_table_unknown_child_fails(self):
        out = _card_dsl.render_card(
            '<card title="t"><table><foo/></table></card>',
            context_json='{"rows": []}',
        )
        self.assertFalse(out["ok"])
        self.assertIn("<table> only holds <col>", out["error"])

    def test_table_is_read_only(self):
        # 报表卡是「看」的:table 编译结果不含任何 callback,handlers 为空。
        out = _card_dsl.render_card(
            '<card title="t"><table source="rows"><col field="a" label="A"/></table></card>',
            context_json='{"rows": [{"a": "1"}]}',
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["handlers"], {})
        dumped = json.dumps(out["card"], ensure_ascii=False)
        self.assertNotIn("behaviors", dumped)


class TestDividerElement(unittest.TestCase):
    def test_divider_compiles_to_hr(self):
        out = _card_dsl.render_card('<card title="t"><divider/></card>')
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["card"]["body"]["elements"], [{"tag": "hr"}])


class TestLeftoverPlaceholder(unittest.TestCase):
    def test_missing_template_value_fails_visible(self):
        # {template} 未填会残留,引擎必须显式报错而不是发一张坏卡。
        values = _mentor_values()
        del values["template"]
        out = _render("mentor-report-card", values, {"rows": []})
        self.assertFalse(out["ok"])
        self.assertIn("template", out["error"])


if __name__ == "__main__":
    unittest.main()
