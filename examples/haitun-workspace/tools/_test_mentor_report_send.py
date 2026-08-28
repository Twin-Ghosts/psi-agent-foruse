# ruff: noqa: RUF001 RUF002 RUF003  # 中文全角标点是刻意排版,非歧义字符
"""T4 mentor 发卡工具单测（feishu_mentor_report_send）。

覆盖：参数校验、分页读取合并、行展开（日期格式化/逾期标红/按负责人分组）、
统计接入 build_mentor_stats、模板渲染、纯只读（handlers 恒空）、测试模式
（参数与环境变量两条覆盖路径）、台账链接拼装、发送调用。

运行方式（unittest 直跑，不用 pytest 收集器——本仓库 pytest addopts 有坑）：
  cd examples/haitun-workspace/tools && PYTHONPATH=. ../../../../.venv/bin/python _test_mentor_report_send.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types
import unittest

# ── stub runtime deps（与 _test_report_cards.py 同套路）───────────────────────
_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TOOLS_DIR)

_stub_impl = types.ModuleType("_todo_card_impl")
_stub_impl._UNDO_ROUNDS = 20
_stub_impl._build_card_from_state = lambda state: {"_state": state, "_legacy_state": state, "schema": "2.0"}
_stub_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_stub_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _stub_impl)

_stub_paths = types.ModuleType("_runtime_paths")
_stub_paths.agent_dir = lambda: os.path.join(_TOOLS_DIR, "..")
sys.modules.setdefault("_runtime_paths", _stub_paths)

# _feishu_impl 全量 stub：_invoke 按分页返回 mock 数据，send_card_impl 捕获调用。
_SENT: list[dict] = []
_PAGES: dict[str, dict] = {}


def _error(message: str, **extra):
    return {"ok": False, "error": message, **extra}


def _query_of(req, key):
    for k, v in getattr(req, "queries", None) or []:
        if k == key:
            return v
    return ""


async def _mock_invoke(req, user_key=None, prefer="tenant", identity="", capabilities=None, retry_rate_limits=True):
    uri = getattr(req, "uri", "") or ""
    if uri.endswith("/records") and getattr(req, "http_method", None).name == "GET":
        page_token = _query_of(req, "page_token")
        return _PAGES.get(page_token, {"ok": True, "data": {"items": [], "has_more": False, "page_token": ""}})
    return _error(f"unexpected request: {uri}")


async def _mock_send_card(
    receive_id, card_json, receive_id_type, user_key=None, business_context_json="{}",
    action_handlers_json="{}", multi_use=False,
):
    _SENT.append(
        {
            "receive_id": receive_id,
            "receive_id_type": receive_id_type,
            "card": json.loads(card_json),
            "business_context": json.loads(business_context_json),
            "action_handlers": json.loads(action_handlers_json),
            "multi_use": multi_use,
        }
    )
    return {"ok": True, "message_id": "om_mock_report"}


_stub_core = types.ModuleType("_feishu_impl")
_stub_core._invoke = _mock_invoke
_stub_core.send_card_impl = _mock_send_card
_stub_core._error = _error
sys.modules.setdefault("_feishu_impl", _stub_core)

import feishu_mentor_report_send as _send  # noqa: E402


def _row(record_id, owner, level, title, status="进行中", score="", due=1785200000000):
    """1785200000000 毫秒 ≈ 2026-07-28；due 传 "" 表示无截止。"""
    fields: dict = {"负责人": [{"id": f"ou_{owner}", "name": owner}], "层级": {"text": level}, "标题": title}
    if status:
        fields["状态"] = {"text": status}
    if score != "":
        fields["mentor打分"] = score
    if due:
        fields["截止日期"] = due
    return {"record_id": record_id, "fields": fields}


def _install_pages(*page_groups):
    """page_groups: [(page_token, items, has_more, next_token), ...]"""
    _PAGES.clear()
    for token, items, has_more, next_token in page_groups:
        _PAGES[token] = {
            "ok": True,
            "data": {"items": items, "has_more": has_more, "page_token": next_token or ""},
        }


def _set_up_default_pages():
    _install_pages(
        (
            "",
            [
                _row("rec1", "张三", "大目标1", "目标A", status="已闭环", score=5),
                _row("rec2", "张三", "todo1", "事项B", status="未闭环逾期", due=1784600000000),
                _row("rec3", "李四", "todo1", "事项C", status="请假顺延"),
            ],
            False,
            "",
        )
    )


def _run(**kwargs):
    _SENT.clear()
    return json.loads(asyncio.run(_send.feishu_mentor_report_send(**kwargs)))


class TestValidation(unittest.TestCase):
    def test_missing_mentor_open_id(self):
        out = _run(mentor_name="赵", cycle_date="2026-08-28", ledger_app_token="t", ledger_table_id="tb")
        self.assertFalse(out["ok"])
        self.assertIn("mentor_open_id", out["error"])

    def test_missing_mentor_name(self):
        out = _run(mentor_open_id="ou_m", cycle_date="2026-08-28", ledger_app_token="t", ledger_table_id="tb")
        self.assertFalse(out["ok"])
        self.assertIn("mentor_name", out["error"])

    def test_missing_cycle_date(self):
        out = _run(mentor_open_id="ou_m", mentor_name="赵", ledger_app_token="t", ledger_table_id="tb")
        self.assertFalse(out["ok"])
        self.assertIn("cycle_date", out["error"])

    def test_missing_ledger_ids(self):
        out = _run(mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28")
        self.assertFalse(out["ok"])
        self.assertIn("ledger_app_token", out["error"])
        out2 = _run(mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28", ledger_app_token="t")
        self.assertFalse(out2["ok"])
        self.assertIn("ledger_table_id", out2["error"])

    def test_bad_expected_people_json(self):
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="t", ledger_table_id="tb", expected_people_json="not-json",
        )
        self.assertFalse(out["ok"])
        self.assertIn("expected_people_json", out["error"])

    def test_expected_people_must_be_array(self):
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="t", ledger_table_id="tb", expected_people_json='{"a":1}',
        )
        self.assertFalse(out["ok"])
        self.assertIn("JSON array", out["error"])


class TestSendFlow(unittest.TestCase):
    def test_basic_send_to_mentor(self):
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_mentor", mentor_name="赵胜迪", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["receive_id"], "ou_mentor")
        self.assertFalse(out["test_override"])
        self.assertEqual(out["row_count"], 3)
        self.assertEqual(out["message_id"], "om_mock_report")
        self.assertEqual(len(_SENT), 1)
        sent = _SENT[0]
        self.assertEqual(sent["receive_id"], "ou_mentor")
        self.assertEqual(sent["receive_id_type"], "open_id")
        self.assertEqual(sent["action_handlers"], {})
        self.assertEqual(sent["business_context"]["kind"], "company_todo_mentor_report")
        self.assertEqual(sent["business_context"]["cycle_date"], "2026-08-28")

    def test_card_header_and_summaries(self):
        _set_up_default_pages()
        _run(
            mentor_open_id="ou_mentor", mentor_name="赵胜迪", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        card = _SENT[0]["card"]
        self.assertEqual(card["header"]["template"], "red")  # 有未闭环逾期 → 红头
        self.assertEqual(card["header"]["title"]["content"], "TODO 报表·08-28·赵胜迪团队")
        texts = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        joined = "\n".join(texts)
        self.assertIn("填报 2 人", joined)
        self.assertIn("请假 1 人", joined)
        self.assertIn("未按时填报 1 人", joined)
        self.assertIn("大目标 1｜TODO 2", joined)
        self.assertIn("已闭环 1｜逾期 1", joined)
        self.assertIn("平均分 5.0｜5分×1", joined)
        self.assertIn("打开本周期台账", joined)
        self.assertIn("https://genuineknowledge.feishu.cn/base/appX?table=tblCYC", joined)

    def test_empty_ledger_renders_empty_fallback(self):
        _install_pages(("", [], False, ""))
        out = _run(
            mentor_open_id="ou_mentor", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["row_count"], 0)
        card = _SENT[0]["card"]
        self.assertEqual(card["header"]["template"], "blue")  # 空表 → 中性蓝
        texts = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        self.assertTrue(any("本周期暂无明细" in t for t in texts))
        self.assertTrue(any("暂无填报" in t for t in texts))
        self.assertTrue(any("暂无打分" in t for t in texts))

    def test_pagination_merges_all_pages(self):
        _install_pages(
            ("", [_row("rec1", "张三", "大目标1", "A")], True, "tok2"),
            ("tok2", [_row("rec2", "李四", "todo1", "B")], True, "tok3"),
            ("tok3", [_row("rec3", "王五", "todo1", "C")], False, ""),
        )
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["row_count"], 3)
        self.assertEqual(out["counts"]["people"]["filled"], 3)

    def test_read_error_reported(self):
        _PAGES.clear()
        _PAGES[""] = {"ok": False, "error": "bitable read denied", "message": "no permission"}
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        self.assertFalse(out["ok"])
        self.assertIn("no permission", out["error"])
        self.assertEqual(_SENT, [])


class TestTableRowExpansion(unittest.TestCase):
    def test_due_date_formatted_mm_dd(self):
        _set_up_default_pages()
        _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        tables = [e for e in _SENT[0]["card"]["body"]["elements"] if e.get("tag") == "table"]
        # 原生 table 一张,3 data rows（按负责人分组：张三两条在前、李四一条）
        self.assertEqual(len(tables), 1)
        rows = tables[0]["rows"]
        self.assertEqual(len(rows), 3)
        row1 = rows[0]
        self.assertEqual(row1["c0"], "大目标1")  # level
        self.assertEqual(row1["c1"], "目标A")  # title
        self.assertRegex(row1["c2"], r"^\d{2}-\d{2}$")  # due MM-DD
        self.assertEqual(row1["c3"], "已闭环")  # status
        self.assertEqual(row1["c4"], "5")  # score

    def test_overdue_red_and_leave_orange(self):
        _set_up_default_pages()
        _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        tables = [e for e in _SENT[0]["card"]["body"]["elements"] if e.get("tag") == "table"]
        statuses = [r["c3"] for r in tables[0]["rows"] if str(r["c3"]).startswith("<font")]
        self.assertEqual(len(statuses), 2)
        self.assertIn("color='red'", statuses[0])
        self.assertIn("未闭环逾期", statuses[0])
        self.assertIn("color='orange'", statuses[1])
        self.assertIn("请假顺延", statuses[1])

    def test_rows_grouped_by_owner(self):
        _install_pages(
            (
                "",
                [
                    _row("r1", "李四", "todo1", "Z事项"),
                    _row("r2", "张三", "大目标1", "A目标"),
                    _row("r3", "张三", "todo1", "B事项"),
                ],
                False,
                "",
            )
        )
        _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        tables = [e for e in _SENT[0]["card"]["body"]["elements"] if e.get("tag") == "table"]
        titles = [r["c1"] for r in tables[0]["rows"] if r["c1"] in ("A目标", "B事项", "Z事项")]
        self.assertEqual(titles, ["A目标", "B事项", "Z事项"])  # 张三两条在前,李四在后


class TestReadOnlyCard(unittest.TestCase):
    def test_no_handlers_no_behaviors(self):
        _set_up_default_pages()
        _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
        )
        dumped = json.dumps(_SENT[0]["card"], ensure_ascii=False)
        self.assertNotIn("behaviors", dumped)
        self.assertEqual(_SENT[0]["action_handlers"], {})


class TestTestMode(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PSI_REPORT_CARD_TEST_RECEIVE_ID", None)

    def test_arg_override(self):
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_mentor", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC", test_receive_id="ou_tester",
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["receive_id"], "ou_tester")
        self.assertTrue(out["test_override"])
        self.assertEqual(_SENT[0]["receive_id"], "ou_tester")

    def test_env_override(self):
        os.environ["PSI_REPORT_CARD_TEST_RECEIVE_ID"] = "ou_env_tester"
        try:
            _set_up_default_pages()
            out = _run(
                mentor_open_id="ou_mentor", mentor_name="赵", cycle_date="2026-08-28",
                ledger_app_token="appX", ledger_table_id="tblCYC",
            )
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(out["receive_id"], "ou_env_tester")
            self.assertTrue(out["test_override"])
        finally:
            os.environ.pop("PSI_REPORT_CARD_TEST_RECEIVE_ID", None)


class TestExpectedPeople(unittest.TestCase):
    def test_unfilled_person_turns_header_red(self):
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
            expected_people_json='["张三", "李四", "王五"]',
        )
        self.assertTrue(out["ok"], out.get("error"))
        card = _SENT[0]["card"]
        self.assertEqual(card["header"]["template"], "red")  # 王五应填未填 → 红头
        self.assertEqual(out["counts"]["people"]["unfilled"], ["王五"])

    def test_open_id_entries_resolved_to_names(self):
        # 名单传 open_id 也能正确判定：ou_张三/ou_李四 已填报,王五 未填。
        _set_up_default_pages()
        out = _run(
            mentor_open_id="ou_m", mentor_name="赵", cycle_date="2026-08-28",
            ledger_app_token="appX", ledger_table_id="tblCYC",
            expected_people_json='["ou_张三", "ou_李四", "王五"]',
        )
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["counts"]["people"]["unfilled"], ["王五"])


if __name__ == "__main__":
    unittest.main()
