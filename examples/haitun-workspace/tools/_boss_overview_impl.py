# ruff: noqa: RUF001 RUF002 RUF003  # 中文全角标点是刻意排版,非歧义字符
"""boss 整体统计卡跨 mentor 合并统计纯函数（T5 统计核心）。

输入是多位 mentor 本周期台账的**合并行数组**（发卡工具逐个台账读回后
拼在一起，每行额外注入 ``_team`` 标记它来自哪个 mentor 团队），输出是
``boss-overview-card`` 模板的卡面 values（``global_summary`` /
``overdue_top`` / ``template``）加上团队维度行（``teams``）与结构化计数。

设计约束（与 ``_report_stats.build_mentor_stats`` 同一套纪律）
------------------------------------------------------------
- 纯函数：不碰网络、不碰飞书、不读配置。所有输入显式传入。
- 容错：行内字段可缺省（缺了当空处理），但行本身必须是 dict（形状错误
  显式报错，不静默丢行）。
- **统一口径，禁止逐表各自统计再相加**：全量行合并后一次遍历，按团队
  分组计数，全局指标由同一套分组结构聚合而来——「团队行数字之和 ==
  全局数字」是一致性不变量（T6 边界测试覆盖）。统计细节（状态六选项、
  层级前缀归类、打分归一）与 ``build_mentor_stats`` 完全同源。
- 团队粒度：按行的 ``_team`` 标记（发卡工具以读取来源 mentor 注入），
  缺省时退回行内 ``mentor`` 人员字段，再缺省归「未分组」——boss 卡永远
  不会因为行缺 mentor 字段就丢行。
- 空团队也出团队行：调用方把本周期 mentor 名单传进来（``mentors``），
  没读到行的 mentor 以零值团队行占位——「团队表行数 == mentor 名单数」
  是验收口径，空团队要看得见而不是消失。

统计口径（与 mentor 摘要卡对齐，写死前对照 SKILL/模板注释）
------------------------------------------------------------
- 团队「人数」= 该团队有台账行的去重负责人数；「请假」= 有请假顺延行的
  去重负责人数（记入 counts，模板团队表未展示此列）；「TODO」= 该团队
  (负责人, 层级标签) 去重的 todo 类条目数；「已闭环」「逾期」= 状态行数；
  「均分」= 非空 mentor打分均值（1 位小数，无打分显示 "—"）。
- 全局指标：大目标（boss 卡叫「目标」）/ 小目标 / TODO 各自 (团队,负责人,
  标签) 去重计数求和；已闭环 / 逾期为状态行数合计；请假顺延为去重负责人
  合计（跨团队天然不相交，求和即全局）。
- 逾期 TOP：状态==未闭环逾期的行，按 (今天−截止日期) 降序取前 top_n，
  文案 ``① 张三(赵胜迪团队)逾期5天``；截止缺失的行排末尾、只标「逾期」
  不标天数；无逾期传 ``"无"``。
- 健康度取色（卡头 template）：red = 有未闭环逾期行；green = 所有团队
  都有行 且 无逾期 且 无进行中/待开始/已交付；blue = 其他。boss 卡拿
  不到「应填未填」名单，取色只看行内状态，不做未填判定。

用法示例
--------
>>> build_boss_stats([
...     {"_team": "赵胜迪", "负责人": {"name": "张三"}, "层级": {"text": "大目标1"},
...      "状态": {"text": "已闭环"}, "mentor打分": 5},
...     {"_team": "李四", "负责人": {"name": "王五"}, "层级": {"text": "todo1"},
...      "状态": {"text": "未闭环逾期"}, "截止日期": 1784000000000},
... ], mentors=[{"open_id": "ou_a", "name": "赵胜迪"}, {"open_id": "ou_b", "name": "李四"}],
...    today=datetime.date(2026, 8, 28))
{'template': 'red', 'global_summary': '目标 1｜TODO 1｜已闭环 1｜逾期 1',
 'overdue_top': '① 王五(李四)逾期…', 'teams': [...], 'counts': {...}}
"""

from __future__ import annotations

import datetime
from collections.abc import Iterable
from typing import Any

import _report_stats as _stats

# ── 状态/层级/打分/人员 处理全部复用 _report_stats 的同一套实现 ───────────────
_row_field = _stats._row_field
_people_key = _stats._people_key
_goal_kind = _stats._goal_kind
_score_value = _stats._score_value
STATUS_LEAVE = _stats.STATUS_LEAVE
STATUS_OVERDUE = _stats.STATUS_OVERDUE
STATUS_CLOSED = _stats.STATUS_CLOSED
_ACTIVE_STATUSES = _stats._ACTIVE_STATUSES

# 逾期 TOP 编号：①②③④⑤⑥⑦⑧⑨⑩（超出用数字兜底）
_CN_NUM: dict[int, str] = {
    1: "①", 2: "②", 3: "③", 4: "④", 5: "⑤",
    6: "⑥", 7: "⑦", 8: "⑧", 9: "⑨", 10: "⑩",
}


def _due_date(value: Any) -> datetime.date | None:
    """Bitable 日期（epoch 毫秒）→ date；空/坏值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value:
        return None
    try:
        return datetime.date.fromtimestamp(int(value) / 1000)
    except (OverflowError, OSError, ValueError):
        return None


def _person_name(field_value: Any) -> str:
    """人员字段取首个 name 用于逾期 TOP 展示；空返回空串。"""
    if isinstance(field_value, list):
        for entry in field_value:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"])
    if isinstance(field_value, dict) and field_value.get("name"):
        return str(field_value["name"])
    return ""


def _team_key(row: dict[str, Any]) -> str:
    """团队归属：优先 _team 注入标记，退回行内 mentor 人员名，再退回未分组。"""
    injected = row.get("_team")
    if isinstance(injected, str) and injected.strip():
        return injected.strip()
    mentor = _person_name(_row_field(row, "mentor"))
    return mentor or "未分组"


def build_boss_stats(
    rows: Iterable[dict[str, Any]],
    mentors: Iterable[dict[str, str]] | None = None,
    today: datetime.date | None = None,
    top_n: int = 5,
    score_round: int = 1,
) -> dict[str, Any]:
    """boss 整体统计卡统计核心（跨 mentor 全量合并）。

    参数
    ----
    rows : 多位 mentor 本周期台账的合并行数组。每行可含 ``_team``（团队
        名，发卡工具注入）、``负责人``（人员）、``mentor``（人员）、
        ``层级``（单选）、``状态``（单选）、``mentor打分``（数字）、
        ``截止日期``（epoch 毫秒）、``标题``（文本）。字段名与
        ``_LEDGER_SCHEMA_FIELDS`` 对齐；缺省字段当空处理，行内值形状
        错误显式报错（行非 dict 抛 TypeError）。
    mentors : 可选。本周期 mentor 名单（``[{"open_id":..., "name":...}]``
        或 ``[{"name":...}]``）。没读到行的 mentor 以零值团队行占位，
        且团队行按此名单顺序排列；名单外又出现在行里的团队排在后面。
    today : 逾期天数基准日期（默认今天）。测试传固定日期保证确定性。
    top_n : 逾期 TOP 条数（默认 5，必须 ≥1）。
    score_round : 均分保留小数位（默认 1）。

    返回
    ----
    dict：卡面 values（template/global_summary/overdue_top）+ 团队行
    （teams）+ 结构化计数（counts），供 T5 发卡工具填模板。
    """
    if not isinstance(rows, (list, tuple)):
        raise TypeError(f"rows must be a list of dicts, got {type(rows).__name__}")
    if top_n < 1:
        raise ValueError(f"top_n must be >= 1, got {top_n}")
    base_date = today or datetime.date.today()

    # ── 单次遍历：按团队分组，统一口径计数 ──────────────────────────────────
    teams: dict[str, dict[str, Any]] = {}
    overdue_items: list[dict[str, Any]] = []

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError(f"row {idx} must be an object, got {type(row).__name__}")
        team = _team_key(row)
        acc = teams.setdefault(
            team,
            {
                "people": set(),
                "leave_people": set(),
                "overdue_people": set(),
                "goal_keys": {"big": set(), "small": set(), "todo": set()},
                "done": {"closed": 0, "active": 0, "overdue": 0},
                "scores": [],
            },
        )
        person = _people_key(_row_field(row, "负责人"))
        if person:
            acc["people"].add(person)
        status = _row_field(row, "状态")
        if status == STATUS_LEAVE and person:
            acc["leave_people"].add(person)
        elif status == STATUS_OVERDUE:
            acc["done"]["overdue"] += 1
            if person:
                acc["overdue_people"].add(person)
            overdue_items.append(
                {
                    "person": _person_name(_row_field(row, "负责人")) or person or "—",
                    "team": team,
                    "due": _due_date(_row_field(row, "截止日期")),
                    "title": str(_row_field(row, "标题") or ""),
                }
            )
        elif status == STATUS_CLOSED:
            acc["done"]["closed"] += 1
        elif status in _ACTIVE_STATUSES:
            acc["done"]["active"] += 1
        # 状态为空/未知：不计数（数据缺状态不阻塞，也不瞎归类）

        kind = _goal_kind(_row_field(row, "层级"))
        label = _row_field(row, "层级")
        if kind is not None and person and label is not None:
            acc["goal_keys"][kind].add((person, str(label).strip()))

        score = _score_value(_row_field(row, "mentor打分"))
        if score is not None:
            acc["scores"].append(score)

    # ── 团队行（mentor 名单先行、名单外团队按出现顺序殿后）─────────────────
    ordered_teams: list[str] = []
    seen: set[str] = set()
    for mentor in mentors or []:
        if not isinstance(mentor, dict):
            continue
        name = str((mentor.get("name") or "").strip())
        if not name:
            name = str((mentor.get("open_id") or "").strip())
        if name and name not in seen:
            ordered_teams.append(name)
            seen.add(name)
    for team in teams:
        if team not in seen:
            ordered_teams.append(team)
            seen.add(team)

    team_rows: list[dict[str, Any]] = []
    n_global = {
        "big": 0, "small": 0, "todo": 0,
        "closed": 0, "active": 0, "overdue": 0,
        "leave_people": 0, "people": 0,
        "scores": [],
    }
    for team in ordered_teams:
        acc = teams.get(team) or {
            "people": set(),
            "leave_people": set(),
            "overdue_people": set(),
            "goal_keys": {"big": set(), "small": set(), "todo": set()},
            "done": {"closed": 0, "active": 0, "overdue": 0},
            "scores": [],
        }
        n_people = len(acc["people"])
        n_leave = len(acc["leave_people"])
        n_todo = len(acc["goal_keys"]["todo"])
        n_closed = acc["done"]["closed"]
        n_overdue = acc["done"]["overdue"]
        scores = acc["scores"]
        avg = round(sum(scores) / len(scores), score_round) if scores else None

        team_rows.append(
            {
                "mentor": team,
                "people": n_people,
                "todo": n_todo,
                "closed": n_closed,
                "overdue": n_overdue,
                "avg_score": f"{avg:.{score_round}f}" if avg is not None else "—",
            }
        )
        n_global["big"] += len(acc["goal_keys"]["big"])
        n_global["small"] += len(acc["goal_keys"]["small"])
        n_global["todo"] += n_todo
        n_global["closed"] += n_closed
        n_global["active"] += acc["done"]["active"]
        n_global["overdue"] += n_overdue
        n_global["leave_people"] += n_leave
        n_global["people"] += n_people
        n_global["scores"].extend(scores)

    # ── 全局指标文案 ─────────────────────────────────────────────────────────
    if n_global["people"] == 0 and n_global["todo"] == 0 and n_global["big"] == 0 \
            and n_global["closed"] == 0 and n_global["overdue"] == 0:
        global_summary = "暂无数据"
    else:
        parts = []
        if n_global["big"]:
            parts.append(f"目标 {n_global['big']}")
        if n_global["small"]:
            parts.append(f"小目标 {n_global['small']}")
        if n_global["todo"]:
            parts.append(f"TODO {n_global['todo']}")
        if n_global["closed"]:
            parts.append(f"已闭环 {n_global['closed']}")
        if n_global["overdue"]:
            parts.append(f"逾期 {n_global['overdue']}")
        if n_global["leave_people"]:
            parts.append(f"请假顺延 {n_global['leave_people']}")
        global_summary = "｜".join(parts)

    # ── 逾期 TOP ─────────────────────────────────────────────────────────────
    def _days_key(item: dict[str, Any]) -> tuple[int, int]:
        """排序键：(有无截止, 逾期天数)。无截止排最后。"""
        if item["due"] is None:
            return (1, 0)
        days = max((base_date - item["due"]).days, 0)
        return (0, -days)

    overdue_items.sort(key=_days_key)
    top = overdue_items[:top_n]
    if not top:
        overdue_top = "无"
    else:
        labels: list[str] = []
        for i, item in enumerate(top, start=1):
            if item["due"] is None:
                labels.append(f"{_CN_NUM[i]} {item['person']}({item['team']})逾期")
            else:
                days = max((base_date - item["due"]).days, 0)
                labels.append(f"{_CN_NUM[i]} {item['person']}({item['team']})逾期{days}天")
        overdue_top = " ".join(labels)

    # ── 卡头取色 ────────────────────────────────────────────────────────────
    if n_global["overdue"] > 0:
        template = "red"
    elif (
        n_global["people"] > 0
        and n_global["overdue"] == 0
        and n_global["active"] == 0
        and all(t["people"] > 0 for t in team_rows)
    ):
        template = "green"
    else:
        template = "blue"

    avg_global = (
        round(sum(n_global["scores"]) / len(n_global["scores"]), score_round)
        if n_global["scores"]
        else None
    )
    return {
        "template": template,
        "global_summary": global_summary,
        "overdue_top": overdue_top,
        "teams": team_rows,
        "counts": {
            "global": {
                "people": n_global["people"],
                "goals": {"big": n_global["big"], "small": n_global["small"], "todo": n_global["todo"]},
                "done": {"closed": n_global["closed"], "active": n_global["active"], "overdue": n_global["overdue"]},
                "leave_people": n_global["leave_people"],
                "scores": {"avg": avg_global, "total": len(n_global["scores"])},
            },
            "teams": {
                team: {
                    "people": len(acc["people"]),
                    "leave_people": len(acc["leave_people"]),
                    "overdue_people": len(acc["overdue_people"]),
                    "goals": {k: len(acc["goal_keys"][k]) for k in ("big", "small", "todo")},
                    "done": dict(acc["done"]),
                    "scores": {
                        "avg": (
                            round(sum(acc["scores"]) / len(acc["scores"]), score_round)
                            if acc["scores"]
                            else None
                        ),
                        "total": len(acc["scores"]),
                    },
                }
                for team, acc in teams.items()
            },
            "overdue_top": [
                {
                    "person": item["person"],
                    "team": item["team"],
                    "days": (
                        None
                        if item["due"] is None
                        else max((base_date - item["due"]).days, 0)
                    ),
                }
                for item in top
            ],
        },
    }

