# ruff: noqa: RUF001 RUF002 RUF003  # 中文全角标点是刻意排版,非歧义字符
"""TODO 报表卡统计口径纯函数（mentor 摘要卡 / boss 总览卡共用的统计核心）。

输入是周期台账行数组（Bitable 记录展开后的 dict，字段名与
``_feishu/mentor_ledger.py`` 的 ``_LEDGER_SCHEMA_FIELDS`` 对齐），输出是
模板卡面文案（``mentor-report-card`` / ``boss-overview-card`` 的 values
占位符）加上结构化计数，供发卡工具（T4/T5）直接填进模板。

设计约束
--------
- 纯函数：不碰网络、不碰飞书、不读配置。所有输入显式传入。
- 容错：行内字段可缺省（缺了当空处理），但行本身必须是 dict（形状错误
  显式报错，不静默丢行——与 ``<table>`` 元素的行为一致）。
- 口径集中在此一处，发卡工具不许另算一套，保证「统计卡数值 = 台账口径」。

统计口径（写死前请对照 SKILL/模板注释确认，改口径要同步改这里）
----------------------------------------------------------------
状态六选项（schema 权威）：待开始 / 进行中 / 已交付 / 已闭环 / 未闭环逾期 /
请假顺延。

- 填报 N 人       = 有台账行的去重负责人数
- 请假 M 人       = 有「请假顺延」行的去重负责人数
- 未按时填报 K 人 = 有「未闭环逾期」行的去重负责人数
  （三块独立计数，一个人可同时出现在多块——信息真实优先于互斥分区）

- 大目标/小目标/todo 数量 = 按 (负责人, 层级标签) 去重计数，层级标签按前缀
  归类（"大目标…" / "小目标…" / "todo…"，前缀不区分大小写）。台账层级
  选项是「同一人内每层级从 1 独立编号」，所以跨人的同名标签（张三的大目标1、
  李四的大目标1）是两个目标，必须带负责人去重，不能只数标签。

- 已闭环 = 状态 == 已闭环 的行数
- 进行中 = 状态 ∈ {待开始, 进行中, 已交付} 的行数（未闭环且未逾期的活跃项）
- 逾期   = 状态 == 未闭环逾期 的行数
  请假顺延行不进这三块（请假是豁免，不是进度状态），只在人员概况体现。

- 平均分 = 非空 mentor打分 的均值（1 位小数，Python ``round`` 银行家舍入，
  如 4.25 → 4.2；counts.scores.avg 存的就是这个显示值）
- 分档   = 按打分值分档计数（整数显示 "5分×3"，非整数显示 "4.5分×1"），
  档位降序；0 分是合法值照算（只过滤 None/空串，不发明过滤规则）
- 打分全空 → "暂无打分"

- 健康度取色（卡头 template）：
  red   = 有「未闭环逾期」行，或（传了 expected_people 时）有应填未填的人
  green = 有行 且 无逾期 且 无进行中/待开始/已交付 且 无未填（请假顺延豁免）
  blue  = 其他（正常进行中）
  行数为 0 且未传 expected_people → blue（空周期中性态，不判红不判绿）

用法示例
--------
>>> build_mentor_stats([
...     {"负责人": "张三", "层级": "大目标1", "状态": "已闭环", "mentor打分": 5},
...     {"负责人": "张三", "层级": "todo1", "状态": "未闭环逾期", "mentor打分": ""},
... ], expected_people=["张三", "李四"])
{'template': 'red', 'people_summary': '填报 1 人｜未按时填报 1 人',
 'goal_summary': '大目标 1｜TODO 1', 'done_summary': '已闭环 1｜逾期 1',
 'score_summary': '平均分 5.0｜5分×1', ...}
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# ── 状态常量（与 _LEDGER_SCHEMA_FIELDS 的 options 一致）──────────────────────
STATUS_TODO = "待开始"
STATUS_DOING = "进行中"
STATUS_DELIVERED = "已交付"
STATUS_CLOSED = "已闭环"
STATUS_OVERDUE = "未闭环逾期"
STATUS_LEAVE = "请假顺延"

_ACTIVE_STATUSES = frozenset({STATUS_TODO, STATUS_DOING, STATUS_DELIVERED})

# ── 层级前缀归类 ─────────────────────────────────────────────────────────────
_GOAL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("大目标", "big"),
    ("小目标", "small"),
    ("todo", "todo"),
)


def _row_field(row: dict[str, Any], key: str) -> Any:
    """取行字段值，字段名可缺省（返回 None）。

    人员字段展开后可能是 {"id": ..., "name": ...} 对象；层级/状态是单选
    对象时取 ``name``。裸字符串原样返回。
    """
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("name") or value.get("id") or value.get("text")
    return value


def _people_key(value: Any) -> str:
    """负责人去重键：优先 open_id/name，退回字符串化。"""
    if isinstance(value, dict):
        for k in ("id", "open_id", "user_id", "name", "text"):
            if value.get(k):
                return str(value[k])
        return str(value)
    if value is None:
        return ""
    return str(value)


def _score_value(value: Any) -> float | None:
    """打分归一：None/空串 → None；数字/数字字符串 → float。其他形状报错。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mentor打分 must be numeric, got {value!r}") from exc


def _fmt_score(value: float) -> str:
    """打分显示：整数去小数点（5 → "5"），非整数保留原样（4.5 → "4.5"）。"""
    if value == int(value):
        return str(int(value))
    return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)


def _goal_kind(level: Any) -> str | None:
    """按前缀把层级标签归到 big/small/todo；认不出返回 None（不计入目标数）。"""
    if level is None:
        return None
    text = str(level).strip().lower()
    for prefix, kind in _GOAL_PREFIXES:
        if text.startswith(prefix):
            return kind
    return None


def build_mentor_stats(
    rows: Iterable[dict[str, Any]],
    expected_people: Iterable[str] | None = None,
    score_round: int = 1,
) -> dict[str, Any]:
    """mentor 摘要卡统计核心。

    参数
    ----
    rows : 本周期台账行数组。每行可含 ``负责人``（人员）、``层级``（单选）、
        ``状态``（单选）、``mentor打分``（数字）。字段名与
        ``_LEDGER_SCHEMA_FIELDS`` 对齐；缺省字段当空处理，行内值形状错误
        显式报错（打分非数字、行非 dict）。
    expected_people : 可选。本周期应填报的负责人名单（姓名/open_id），
        用于判断「应填未填」→ 卡头取红。未传则不启用未填判定。
    score_round : 平均分保留小数位（默认 1）。

    返回
    ----
    dict：卡面 values（template/people_summary/goal_summary/done_summary/
    score_summary）＋结构化计数（counts），供 T4 发卡工具填模板。
    """
    if not isinstance(rows, (list, tuple)):
        raise TypeError(f"rows must be a list of dicts, got {type(rows).__name__}")

    people_seen: set[str] = set()          # 有行的负责人
    people_leave: set[str] = set()         # 有请假顺延行的负责人
    people_overdue: set[str] = set()       # 有未闭环逾期行的负责人
    goal_keys: dict[str, set[tuple[str, str]]] = {"big": set(), "small": set(), "todo": set()}
    done_counts = {"closed": 0, "active": 0, "overdue": 0}
    scores: list[float] = []

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError(f"row {idx} must be an object, got {type(row).__name__}")
        person = _people_key(_row_field(row, "负责人"))
        if person:
            people_seen.add(person)
        status = _row_field(row, "状态")
        if status == STATUS_LEAVE and person:
            people_leave.add(person)
        elif status == STATUS_OVERDUE:
            done_counts["overdue"] += 1
            if person:
                people_overdue.add(person)
        elif status == STATUS_CLOSED:
            done_counts["closed"] += 1
        elif status in _ACTIVE_STATUSES:
            done_counts["active"] += 1
        # 状态为空/未知：不计数（数据缺状态不阻塞，也不瞎归类）

        kind = _goal_kind(_row_field(row, "层级"))
        if kind is not None and person:
            goal_keys[kind].add((person, str(_row_field(row, "层级")).strip()))

        score = _score_value(_row_field(row, "mentor打分"))
        if score is not None:
            scores.append(score)

    # 未填判定（可选）
    unfilled: list[str] = []
    if expected_people is not None:
        for name in expected_people:
            key = _people_key(name)
            if key and key not in people_seen:
                unfilled.append(str(name))

    # ── 卡头取色 ────────────────────────────────────────────────────────────
    has_overdue = done_counts["overdue"] > 0
    has_unfilled = bool(unfilled)
    all_closed = (
        bool(people_seen)
        and not has_overdue
        and done_counts["active"] == 0
        and not has_unfilled
    )
    template = "red" if (has_overdue or has_unfilled) else ("green" if all_closed else "blue")

    # ── 人员概况 ────────────────────────────────────────────────────────────
    n_filled, n_leave, n_overdue = (
        len(people_seen),
        len(people_leave),
        len(people_overdue),
    )
    if n_filled == 0:
        people_summary = "暂无填报"
    else:
        parts = [f"填报 {n_filled} 人"]
        if n_leave:
            parts.append(f"请假 {n_leave} 人")
        if n_overdue:
            parts.append(f"未按时填报 {n_overdue} 人")
        people_summary = "｜".join(parts)

    # ── 目标数量 ────────────────────────────────────────────────────────────
    n_big, n_small, n_todo = (
        len(goal_keys["big"]),
        len(goal_keys["small"]),
        len(goal_keys["todo"]),
    )
    if n_big == n_small == n_todo == 0:
        goal_summary = "暂无目标"
    else:
        parts = []
        if n_big:
            parts.append(f"大目标 {n_big}")
        if n_small:
            parts.append(f"小目标 {n_small}")
        if n_todo:
            parts.append(f"TODO {n_todo}")
        goal_summary = "｜".join(parts)

    # ── 完成情况 ────────────────────────────────────────────────────────────
    n_closed, n_active, n_overdue_rows = (
        done_counts["closed"],
        done_counts["active"],
        done_counts["overdue"],
    )
    if n_closed == n_active == n_overdue_rows == 0:
        done_summary = "暂无明细"
    else:
        parts = []
        if n_closed:
            parts.append(f"已闭环 {n_closed}")
        if n_active:
            parts.append(f"进行中 {n_active}")
        if n_overdue_rows:
            parts.append(f"逾期 {n_overdue_rows}")
        done_summary = "｜".join(parts)

    # ── 评价概况 ────────────────────────────────────────────────────────────
    if scores:
        avg = round(sum(scores) / len(scores), score_round)
        buckets: dict[float, int] = {}
        for s in scores:
            buckets[s] = buckets.get(s, 0) + 1
        bucket_text = "｜".join(
            f"{_fmt_score(v)}分×{n}" for v, n in sorted(buckets.items(), reverse=True)
        )
        score_summary = f"平均分 {avg:.{score_round}f}｜{bucket_text}"
    else:
        avg = None
        score_summary = "暂无打分"

    return {
        "template": template,
        "people_summary": people_summary,
        "goal_summary": goal_summary,
        "done_summary": done_summary,
        "score_summary": score_summary,
        "counts": {
            "people": {"filled": n_filled, "leave": n_leave, "overdue": n_overdue, "unfilled": unfilled},
            "goals": {"big": n_big, "small": n_small, "todo": n_todo},
            "done": {"closed": n_closed, "active": n_active, "overdue": n_overdue_rows},
            "scores": {"avg": avg, "total": len(scores), "buckets": buckets if scores else {}},
        },
    }
