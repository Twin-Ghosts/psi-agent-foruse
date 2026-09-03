# ruff: noqa: RUF001 RUF002 RUF003  # 中文全角标点是刻意排版,非歧义字符
"""T5 boss 统计卡单测（纯函数 build_boss_stats + 工具 feishu_boss_overview_send）。

纯函数覆盖：多团队合并/空团队占位/请假豁免/均分/逾期 TOP 排序与截断/
绿色取色/行形状报错/团队归属回退。
工具覆盖：参数校验、ensure→cycle_table→读表 mock 全链路、分页合并、
卡面文案与团队表、纯只读、测试模式（参数与环境变量两条覆盖路径）、
mentor 读表失败整卡失败、空周期团队零值占位。

运行方式（unittest 直跑，不用 pytest 收集器——本仓库 pytest addopts 有坑）：
  cd examples/haitun-workspace/tools && PYTHONPATH=. ../../../../.venv/bin/python _test_boss_overview_send.py
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import sys
import types
import unittest

_TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _TOOLS_DIR)

# ── stub runtime deps（与 _test_report_cards.py / _test_mentor_report_send.py 同套路）──
_stub_impl = types.ModuleType("_todo_card_impl")
_stub_impl._UNDO_ROUNDS = 20
_stub_impl._build_card_from_state = lambda state: {"_state": state, "_legacy_state": state, "schema": "2.0"}
_stub_impl._tick_action_id = lambda i, r: f"todo_tick_{i}_r{r}"
_stub_impl._untick_action_id = lambda i, r: f"todo_untick_{i}_r{r}"
sys.modules.setdefault("_todo_card_impl", _stub_impl)

_stub_paths = types.ModuleType("_runtime_paths")
_stub_paths.agent_dir = lambda: os.path.join(_TOOLS_DIR, "..")
sys.modules.setdefault("_runtime_paths", _stub_paths)

# ── mock 数据区 ──────────────────────────────────────────────────────────────
# _DRIVE[folder_token] = 该文件夹下的文件列表（ensure 找 "TODO 台账-<name>"）
# _TABLES[app_token]   = 该 base 的表列表（ensure 首表 + cycle_table 找本周期表）
# _PAGES[(app, tbl)]   = 分页表：page_token -> {"items", "has_more", "next"}
_DRIVE: dict[str, list] = {}
_TABLES: dict[str, list] = {}
_PAGES: dict[tuple[str, str], dict[str, dict]] = {}
_SENT: list[dict] = []


def _error(message: str, **extra):
    return {"ok": False, "error": message, **extra}


def _dumps_result(result):
    return json.dumps(result, ensure_ascii=False)


def _query_of(req, key):
    for k, v in getattr(req, "queries", None) or []:
        if k == key:
            return v
    return ""


async def _mock_invoke(req, user_key=None, prefer="tenant", identity="", capabilities=None, retry_rate_limits=True):
    uri = getattr(req, "uri", "") or ""
    method = getattr(getattr(req, "http_method", None), "name", "") or ""
    paths = getattr(req, "paths", None) or {}

    if uri.endswith("/files") and method == "GET":  # drive 文件夹列表（ensure）
        folder_token = _query_of(req, "folder_token")
        return {
            "ok": True,
            "data": {
                "files": _DRIVE.get(folder_token, []),
                "has_more": False,
                "page_token": "",
            },
        }

    if uri == "/open-apis/bitable/v1/apps/:app_token/tables" and method == "GET":
        app_token = paths.get("app_token", "")
        return {
            "ok": True,
            "data": {
                "items": _TABLES.get(app_token, []),
                "has_more": False,
                "page_token": "",
            },
        }

    if uri.endswith("/records") and method == "GET":
        app_token = paths.get("app_token", "")
        table_id = paths.get("table_id", "")
        page_token = _query_of(req, "page_token")
        pages = _PAGES.get((app_token, table_id), {})
        page = pages.get(page_token, {"items": [], "has_more": False, "next": ""})
        return {
            "ok": True,
            "data": {
                "items": page["items"],
                "has_more": page.get("has_more", False),
                "page_token": page.get("next", "") or "",
            },
        }

    if uri.endswith("/members") and method == "POST":  # ensure 授权（mentor/boss）
        return {"ok": True, "data": {"member": {"member_id": "ou_x"}}}

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
    return {"ok": True, "message_id": "om_mock_boss"}


_stub_core = types.ModuleType("_feishu_impl")
_stub_core._invoke = _mock_invoke
_stub_core.send_card_impl = _mock_send_card
_stub_core._error = _error
_stub_core.dumps_result = _dumps_result
sys.modules.setdefault("_feishu_impl", _stub_core)

import _boss_overview_impl as _boss  # noqa: E402
import feishu_boss_overview_send as _send  # noqa: E402


# ── 构造数据 helper ──────────────────────────────────────────────────────────
def _due_ms(d: datetime.date) -> int:
    """date → epoch 毫秒（测试用固定基准）。"""
    return int(datetime.datetime(d.year, d.month, d.day).timestamp() * 1000)


def _row(owner, level, title, status="进行中", score="", due=None, team=None, mentor=None):
    """台账行形状：负责人为人员数组，层级/状态为单选对象，_team 为注入标记。"""
    fields: dict = {
        "负责人": [{"id": f"ou_{owner}", "name": owner}],
        "层级": {"text": level},
        "标题": title,
    }
    if status:
        fields["状态"] = {"text": status}
    if score != "":
        fields["mentor打分"] = score
    if due is not None:
        fields["截止日期"] = due
    if mentor is not None:
        fields["mentor"] = [{"id": f"ou_{mentor}", "name": mentor}]
    if team is not None:
        fields["_team"] = team
    return fields


TODAY = datetime.date(2026, 8, 28)
MENTORS = [{"open_id": "ou_zhao", "name": "赵胜迪"}, {"open_id": "ou_li", "name": "李四"}]


def _two_team_rows():
    """赵胜迪团队：已闭环 1 + 逾期 1（截止 8-23，逾期 5 天）；李四团队：进行中 1。"""
    return [
        _row("张三", "大目标1", "目标A", status="已闭环", score=5, team="赵胜迪"),
        _row("张三", "todo1", "事项B", status="未闭环逾期", due=_due_ms(datetime.date(2026, 8, 23)), team="赵胜迪"),
        _row("王五", "todo1", "事项C", status="进行中", team="李四"),
    ]


# ═══════════════════════════ 纯函数：build_boss_stats ═══════════════════════════


class TestBuildBossStats(unittest.TestCase):
    def test_two_team_merge_counts(self):
        out = _boss.build_boss_stats(_two_team_rows(), mentors=MENTORS, today=TODAY)
        self.assertEqual(out["template"], "red")  # 有逾期 → 红头
        # 全局指标：大目标1 + TODO2 + 已闭环1 + 逾期1
        self.assertEqual(out["global_summary"], "目标 1｜TODO 2｜已闭环 1｜逾期 1")
        # 逾期 TOP：张三(赵胜迪) 逾期 5 天
        self.assertEqual(out["overdue_top"], "① 张三(赵胜迪)逾期5天")
        # 团队行数与 mentor 名单一致，顺序一致
        self.assertEqual([t["mentor"] for t in out["teams"]], ["赵胜迪", "李四"])
        zhao = out["teams"][0]
        self.assertEqual(zhao["people"], 1)
        self.assertEqual(zhao["todo"], 1)
        self.assertEqual(zhao["closed"], 1)
        self.assertEqual(zhao["overdue"], 1)
        self.assertEqual(zhao["avg_score"], "5.0")
        li = out["teams"][1]
        self.assertEqual(li["people"], 1)
        self.assertEqual(li["todo"], 1)
        self.assertEqual(li["closed"], 0)
        self.assertEqual(li["overdue"], 0)
        self.assertEqual(li["avg_score"], "—")
        # 一致性不变量：团队行求和 == 全局
        c = out["counts"]["global"]
        self.assertEqual(c["goals"]["big"], 1)
        self.assertEqual(c["goals"]["todo"], 2)
        self.assertEqual(c["done"]["closed"], 1)
        self.assertEqual(c["done"]["overdue"], 1)
        self.assertEqual(c["people"], 2)

    def test_empty_roster_zero_team_rows(self):
        # 两个 mentor 都无行 → 零值团队行占位，全局暂无数据，中性蓝
        out = _boss.build_boss_stats([], mentors=MENTORS, today=TODAY)
        self.assertEqual(out["template"], "blue")
        self.assertEqual(out["global_summary"], "暂无数据")
        self.assertEqual(out["overdue_top"], "无")
        self.assertEqual([t["mentor"] for t in out["teams"]], ["赵胜迪", "李四"])
        for t in out["teams"]:
            self.assertEqual(t["people"], 0)
            self.assertEqual(t["todo"], 0)
            self.assertEqual(t["avg_score"], "—")

    def test_empty_team_in_roster_gets_zero_row(self):
        rows = [_row("张三", "todo1", "事项A", status="已闭环", team="赵胜迪")]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual(len(out["teams"]), 2)
        self.assertEqual(
            out["teams"][1],
            {"mentor": "李四", "people": 0, "todo": 0, "closed": 0, "overdue": 0, "avg_score": "—"},
        )

    def test_leave_exempt_from_done_counts(self):
        rows = [
            _row("张三", "todo1", "事项A", status="请假顺延", team="赵胜迪"),
            _row("李四", "todo1", "事项B", status="已闭环", team="赵胜迪"),
        ]
        out = _boss.build_boss_stats(rows, mentors=[{"open_id": "ou_zhao", "name": "赵胜迪"}], today=TODAY)
        c = out["counts"]["global"]
        self.assertEqual(c["done"]["closed"], 1)  # 请假行不进已闭环/进行中/逾期
        self.assertEqual(c["done"]["overdue"], 0)
        self.assertEqual(c["leave_people"], 1)
        # 无逾期、无进行中、团队有行 → 全员闭环绿头
        self.assertEqual(out["template"], "green")

    def test_green_only_when_all_closed(self):
        rows = [
            _row("张三", "todo1", "事项A", status="已闭环", team="赵胜迪"),
            _row("李四", "todo1", "事项B", status="已闭环", team="李四"),
        ]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual(out["template"], "green")

    def test_active_keeps_blue(self):
        rows = [_row("张三", "todo1", "事项A", status="进行中", team="赵胜迪")]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual(out["template"], "blue")

    def test_score_averages_and_missing(self):
        rows = [
            _row("张三", "todo1", "A", status="已闭环", score=5, team="赵胜迪"),
            _row("李四", "todo1", "B", status="已闭环", score=4.5, team="赵胜迪"),
        ]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual(out["teams"][0]["avg_score"], "4.8")  # (5+4.5)/2 = 4.75 → round 4.8
        c = out["counts"]["global"]["scores"]
        self.assertEqual(c["avg"], 4.8)
        self.assertEqual(c["total"], 2)

    def test_overdue_top_ordering_and_truncation(self):
        rows = [
            # 8 天
            _row("张三", "todo1", "A", status="未闭环逾期", due=_due_ms(datetime.date(2026, 8, 20)), team="赵胜迪"),
            # 3 天
            _row("李四", "todo1", "B", status="未闭环逾期", due=_due_ms(datetime.date(2026, 8, 25)), team="李四"),
            # 无截止 → 排末尾
            _row("王五", "todo1", "C", status="未闭环逾期", team="赵胜迪"),
        ]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY, top_n=2)
        self.assertEqual(out["overdue_top"], "① 张三(赵胜迪)逾期8天 ② 李四(李四)逾期3天")
        top = out["counts"]["overdue_top"]
        self.assertEqual([t["person"] for t in top], ["张三", "李四"])
        self.assertEqual([t["days"] for t in top], [8, 3])
        # 无截止那条被截断（top_n=2）
        self.assertNotIn("王五", out["overdue_top"])

    def test_overdue_missing_due_lasts(self):
        rows = [
            _row("张三", "todo1", "A", status="未闭环逾期", due=_due_ms(datetime.date(2026, 8, 20)), team="赵胜迪"),
            _row("王五", "todo1", "C", status="未闭环逾期", team="赵胜迪"),
        ]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertIn("① 张三(赵胜迪)逾期8天 ② 王五(赵胜迪)逾期", out["overdue_top"])
        self.assertEqual(out["counts"]["overdue_top"][1]["days"], None)

    def test_no_overdue_says_wu(self):
        rows = [_row("张三", "todo1", "A", status="已闭环", team="赵胜迪")]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual(out["overdue_top"], "无")

    def test_team_fallback_to_mentor_field_and_unknown(self):
        # 无 _team 但有 mentor 人员字段 → 按 mentor 名分组；两者皆无 → 未分组
        rows = [
            _row("张三", "todo1", "A", status="已闭环", mentor="赵胜迪"),
            _row("李四", "todo1", "B", status="已闭环"),
        ]
        out = _boss.build_boss_stats(rows, today=TODAY)
        names = [t["mentor"] for t in out["teams"]]
        self.assertEqual(names, ["赵胜迪", "未分组"])
        self.assertEqual(out["teams"][0]["people"], 1)
        self.assertEqual(out["teams"][1]["people"], 1)

    def test_non_dict_row_raises(self):
        with self.assertRaises(TypeError):
            _boss.build_boss_stats(["not-a-dict"], mentors=MENTORS, today=TODAY)

    def test_bad_top_n_raises(self):
        with self.assertRaises(ValueError):
            _boss.build_boss_stats([], mentors=MENTORS, today=TODAY, top_n=0)

    def test_mentor_list_order_wins_over_row_order(self):
        rows = [_row("王五", "todo1", "C", status="已闭环", team="李四")]
        out = _boss.build_boss_stats(rows, mentors=MENTORS, today=TODAY)
        self.assertEqual([t["mentor"] for t in out["teams"]], ["赵胜迪", "李四"])


# ═══════════════════════════ 工具：feishu_boss_overview_send ═══════════════════════════

FOLDER = "fol_ledgers"


def _install_mentor(app_token: str, table_id: str, mentor_name: str, cycle_rows: list[dict]):
    """把一位 mentor 的台账装进 mock：云盘文件 + base 表 + 本周期表行。"""
    _DRIVE.setdefault(FOLDER, []).append(
        {"type": "bitable", "name": f"TODO 台账-{mentor_name}", "token": app_token}
    )
    _TABLES[app_token] = [
        {"table_id": "tbl_default", "name": "台账"},
        {"table_id": table_id, "name": "台账-2026-08-28"},
    ]
    _PAGES[(app_token, table_id)] = {"": {"items": cycle_rows, "has_more": False, "next": ""}}


def _install_two_mentors():
    _DRIVE.clear()
    _TABLES.clear()
    _PAGES.clear()
    _install_mentor("appZHAO", "tblZHAOCYC", "赵胜迪", [
        {
            "record_id": "rec1",
            "fields": {
                "负责人": [{"id": "ou_张三", "name": "张三"}],
                "层级": {"text": "大目标1"},
                "标题": "目标A",
                "状态": {"text": "已闭环"},
                "mentor打分": 5,
                "截止日期": _due_ms(datetime.date(2026, 8, 25)),
            },
        },
        {
            "record_id": "rec2",
            "fields": {
                "负责人": [{"id": "ou_张三", "name": "张三"}],
                "层级": {"text": "todo1"},
                "标题": "事项B",
                "状态": {"text": "未闭环逾期"},
                "截止日期": _due_ms(datetime.date(2026, 8, 23)),
            },
        },
    ])
    _install_mentor("appLI", "tblLICYC", "李四", [
        {
            "record_id": "rec3",
            "fields": {
                "负责人": [{"id": "ou_王五", "name": "王五"}],
                "层级": {"text": "todo1"},
                "标题": "事项C",
                "状态": {"text": "进行中"},
            },
        },
    ])


def _run(**kwargs):
    _SENT.clear()
    defaults = {
        "boss_open_id": "ou_boss",
        "cycle_date": "2026-08-28",
        "mentors_json": json.dumps(MENTORS, ensure_ascii=False),
        "folder_token": FOLDER,
        "tree_url": "https://genuineknowledge.feishu.cn/wiki/WIKIROOT",
        # 固定统计基准日：逾期天数断言不随真实运行日期漂移
        "today_iso": "2026-08-28",
    }
    defaults.update(kwargs)
    return json.loads(asyncio.run(_send.feishu_boss_overview_send(**defaults)))


class TestToolValidation(unittest.TestCase):
    def test_missing_boss_open_id(self):
        _install_two_mentors()
        out = _run(boss_open_id="")
        self.assertFalse(out["ok"])
        self.assertIn("boss_open_id", out["error"])

    def test_missing_cycle_date(self):
        _install_two_mentors()
        out = _run(cycle_date="")
        self.assertFalse(out["ok"])
        self.assertIn("cycle_date", out["error"])

    def test_missing_folder_token(self):
        _install_two_mentors()
        out = _run(folder_token="")
        self.assertFalse(out["ok"])
        self.assertIn("folder_token", out["error"])

    def test_missing_tree_url(self):
        _install_two_mentors()
        out = _run(tree_url="")
        self.assertFalse(out["ok"])
        self.assertIn("tree_url", out["error"])

    def test_bad_mentors_json(self):
        _install_two_mentors()
        out = _run(mentors_json="not-json")
        self.assertFalse(out["ok"])
        self.assertIn("mentors_json", out["error"])
        out2 = _run(mentors_json='{"a":1}')
        self.assertFalse(out2["ok"])
        self.assertIn("JSON array", out2["error"])
        out3 = _run(mentors_json='["赵胜迪"]')
        self.assertFalse(out3["ok"])
        self.assertIn("object", out3["error"])

    def test_empty_mentors_list(self):
        _install_two_mentors()
        out = _run(mentors_json="[]")
        self.assertFalse(out["ok"])
        self.assertIn("must not be empty", out["error"])

    def test_bad_top_n(self):
        _install_two_mentors()
        out = _run(top_n=0)
        self.assertFalse(out["ok"])
        self.assertIn("top_n", out["error"])
        out2 = _run(top_n="abc")
        self.assertFalse(out2["ok"])
        self.assertIn("top_n", out2["error"])


class TestToolSendFlow(unittest.TestCase):
    def test_basic_send_to_boss(self):
        _install_two_mentors()
        out = _run()
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["receive_id"], "ou_boss")
        self.assertFalse(out["test_override"])
        self.assertEqual(out["team_count"], 2)
        self.assertEqual(out["row_count"], 3)
        self.assertEqual(out["mentor_errors"], [])
        self.assertEqual(out["message_id"], "om_mock_boss")
        self.assertEqual(len(_SENT), 1)
        sent = _SENT[0]
        self.assertEqual(sent["receive_id"], "ou_boss")
        self.assertEqual(sent["receive_id_type"], "open_id")
        self.assertEqual(sent["action_handlers"], {})
        self.assertEqual(sent["business_context"]["kind"], "company_todo_boss_overview")
        self.assertEqual(sent["business_context"]["cycle_date"], "2026-08-28")
        self.assertEqual(sent["business_context"]["mentor_count"], 2)

    def test_card_header_global_and_team_table(self):
        _install_two_mentors()
        _run()
        card = _SENT[0]["card"]
        self.assertEqual(card["header"]["template"], "red")  # 有逾期 → 红头
        self.assertEqual(card["header"]["title"]["content"], "全公司 TODO 总览·08-28")
        texts = [e["content"] for e in card["body"]["elements"] if e.get("tag") == "markdown"]
        joined = "\n".join(texts)
        self.assertIn("目标 1｜TODO 2｜已闭环 1｜逾期 1", joined)
        self.assertIn("① 张三(赵胜迪)逾期5天", joined)
        self.assertIn("打开公司工作树", joined)
        self.assertIn("https://genuineknowledge.feishu.cn/wiki/WIKIROOT", joined)
        # 团队表：原生 table 一张,2 行
        tables = [e for e in card["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        table = tables[0]
        self.assertEqual(
            [c["display_name"] for c in table["columns"]],
            ["团队", "人数", "TODO", "已闭环", "逾期", "均分"],
        )
        zhao = table["rows"][0]
        self.assertEqual([zhao[f"c{i}"] for i in range(5)], ["赵胜迪", "1", "1", "1", "1"])
        self.assertEqual(zhao["c5"], "5.0")
        li = table["rows"][1]
        self.assertEqual([li[f"c{i}"] for i in range(6)], ["李四", "1", "1", "0", "0", "—"])

    def test_empty_cycle_team_zero_row(self):
        # 李四本周期无行 → 零值团队行，团队表行数仍与名单一致
        _install_two_mentors()
        _PAGES[("appLI", "tblLICYC")] = {"": {"items": [], "has_more": False, "next": ""}}
        out = _run()
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["row_count"], 2)
        self.assertEqual(out["team_count"], 2)
        tables = [e for e in _SENT[0]["card"]["body"]["elements"] if e.get("tag") == "table"]
        self.assertEqual(len(tables), 1)
        li = tables[0]["rows"][1]
        self.assertEqual([li[f"c{i}"] for i in range(6)], ["李四", "0", "0", "0", "0", "—"])

    def test_pagination_merges_per_mentor(self):
        _install_two_mentors()
        _PAGES[("appZHAO", "tblZHAOCYC")] = {
            "": {
                "items": [{"record_id": "r1", "fields": _row("张三", "todo1", "A", status="已闭环", team="赵胜迪")}],
                "has_more": True,
                "next": "tok2",
            },
            "tok2": {
                "items": [{"record_id": "r2", "fields": _row("李四", "todo1", "B", status="已闭环", team="赵胜迪")}],
                "has_more": False,
                "next": "",
            },
        }
        out = _run()
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["row_count"], 3)  # 赵2(分页) + 李1
        self.assertEqual(out["counts"]["global"]["done"]["closed"], 2)

    def test_read_only_card(self):
        _install_two_mentors()
        _run()
        dumped = json.dumps(_SENT[0]["card"], ensure_ascii=False)
        self.assertNotIn("behaviors", dumped)
        self.assertEqual(_SENT[0]["action_handlers"], {})

    def test_mentor_read_error_fails_whole_card(self):
        # 李四的台账 base 不存在 → ensure 找不到 → 整卡失败，不发部分卡
        _install_two_mentors()
        _DRIVE[FOLDER] = [f for f in _DRIVE[FOLDER] if f["name"] != "TODO 台账-李四"]
        out = _run()
        self.assertFalse(out["ok"])
        self.assertIn("mentor_errors", out)
        self.assertEqual(out["mentor_errors"][0]["mentor"], "李四")
        self.assertEqual(_SENT, [])  # 不发部分卡

    def test_mentor_without_open_id_errors(self):
        _install_two_mentors()
        out = _run(mentors_json=json.dumps([{"name": "赵胜迪"}, {"name": "李四"}], ensure_ascii=False))
        self.assertFalse(out["ok"])
        self.assertEqual(len(out["mentor_errors"]), 2)  # open_id 缺失 → ensure 拒绝


class TestToolTestMode(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("PSI_BOSS_CARD_TEST_RECEIVE_ID", None)
        os.environ.pop("PSI_REPORT_CARD_TEST_RECEIVE_ID", None)

    def test_arg_override(self):
        _install_two_mentors()
        out = _run(test_receive_id="ou_tester")
        self.assertTrue(out["ok"], out.get("error"))
        self.assertEqual(out["receive_id"], "ou_tester")
        self.assertTrue(out["test_override"])
        self.assertEqual(_SENT[0]["receive_id"], "ou_tester")

    def test_env_override(self):
        os.environ["PSI_BOSS_CARD_TEST_RECEIVE_ID"] = "ou_env_tester"
        try:
            _install_two_mentors()
            out = _run()
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(out["receive_id"], "ou_env_tester")
            self.assertTrue(out["test_override"])
        finally:
            os.environ.pop("PSI_BOSS_CARD_TEST_RECEIVE_ID", None)

    def test_fallback_env_override(self):
        # T4 的 PSI_REPORT_CARD_TEST_RECEIVE_ID 也能改投 boss 卡
        os.environ["PSI_REPORT_CARD_TEST_RECEIVE_ID"] = "ou_report_tester"
        try:
            _install_two_mentors()
            out = _run()
            self.assertTrue(out["ok"], out.get("error"))
            self.assertEqual(out["receive_id"], "ou_report_tester")
        finally:
            os.environ.pop("PSI_REPORT_CARD_TEST_RECEIVE_ID", None)


if __name__ == "__main__":
    unittest.main()
