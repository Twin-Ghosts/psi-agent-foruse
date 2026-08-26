"""Feishu Leave (请假) — read the todo board's "请假表" sub-sheet and judge date overlaps.

Split out of ``_feishu_impl.py`` by domain, following the same shape as
``attendance.py`` / ``sheet.py``. This module reaches the shared client/token
layer through ``_core`` so that everything patched on ``_feishu_impl``
(``_invoke``, ``_get_client``, ``_get_valid_uat``, ...) keeps taking effect
here.

There is no Feishu endpoint that answers "who is on leave" — ``/approval/v4/instances``
only supports creating an instance or looking one up by id (no per-person enumeration),
and ``attendance:task:readonly`` returns clock results (Normal/Late/Early/Lack), not leave
records. So leave is tracked as a plain sub-sheet next to the todo board (same
``sheet_token``, a "请假表" worksheet: 姓名/开始日期/结束日期/类型/是否整天/备注), and this
module does the one thing that must not be left to a model: date-interval overlap
judgment. Getting that wrong once mis-marks someone's leave status for a whole cycle.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import _feishu_impl as _core

# Column header synonyms tolerate minor label drift without hardcoding a fixed layout —
# same discipline as feishu-todo-board-sync: structure is discovered each run.
_HEADER_SYNONYMS: dict[str, tuple[str, ...]] = {
    "name": ("姓名", "名字", "负责人"),
    "start": ("开始日期", "起始日期", "开始时间"),
    "end": ("结束日期", "截止日期", "结束时间"),
    "type": ("类型", "请假类型", "假期类型"),
    # 整天判定优先读「是否整天」列;没有该列时用「时长(天)」推断
    # (时长 ≥ 1 = 整天, 0.5 = 半天)。
    "full_day": ("是否整天", "整天", "全天"),
    "duration": ("时长",),
    # 时段:开始时段=上午 → 从当天上午起休(当天整天);下午 → 从当天下午起休(半天)。
    # 结束时段=上午 → 结束日只休上午(半天);下午 → 休到下午(整天)。
    "start_period": ("开始时段",),
    "end_period": ("结束时段",),
    "note": ("备注", "说明", "请假事由"),
}

# Feishu Sheets' values API returns a date-formatted cell as an Excel/Lotus serial
# number (days since 1899-12-30, the same epoch Excel uses) rather than "YYYY-MM-DD" —
# a plain str() of the raw cell would silently misparse every date in the column.
_SERIAL_EPOCH = date(1899, 12, 30)


def _parse_cell_date(raw: str) -> date | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        pass
    try:
        serial = float(text)
    except ValueError:
        return None
    try:
        return _SERIAL_EPOCH + timedelta(days=int(serial))
    except OverflowError, ValueError:
        return None


def _match_header(cell: str, keys: tuple[str, ...]) -> bool:
    normalized = cell.strip()
    return any(normalized == k or k in normalized for k in keys)


def _index_columns(header_row: list[str]) -> dict[str, int]:
    columns: dict[str, int] = {}
    for field, synonyms in _HEADER_SYNONYMS.items():
        for idx, cell in enumerate(header_row):
            if _match_header(cell, synonyms):
                columns[field] = idx
                break
    return columns


def _parse_full_day(raw: str) -> bool:
    text = raw.strip().casefold()
    return text in ("是", "true", "1", "yes", "y", "整天", "全天")


def _strip_mention(raw: str) -> str:
    """姓名归一化:去掉 @ 前缀与首尾空白。

    请假表的姓名单元格常带 @ 前缀(飞书表格里 @人 的格式,如「@黄子建」),
    而调用方 names_json 通常传纯文本名单 —— 直接字符串比对会全部漏判,
    所以在过滤时两侧都归一化,只影响读取匹配,不改动表格数据。
    """
    return raw.strip().lstrip("@").strip()


def _parse_duration(raw: str) -> float | None:
    text = raw.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _is_full_day(full_day_cell: str, duration_cell: str, span_days: int) -> bool:
    """整天判定,优先级:「是否整天」列 > 「时长(天)」列 > 按跨度默认。

    时长 ≥ 1 = 整天(3 → 3 整天),0.5 = 半天(2.5 = 2 整天 + 半天,整条记录
    含半天故不算整天——半天当天仍可干活,按「部分日期请假」处理更保守)。
    两列都缺失时:多天假默认整天、单天假默认非整天。
    """
    if full_day_cell.strip():
        return _parse_full_day(full_day_cell)
    duration = _parse_duration(duration_cell)
    if duration is not None:
        # 时长要跟跨度天数比,不是跟 1 比:span=3、时长 2.5 → 含半天 → 非整天;
        # 时长 3 ≥ span 3 → 整段整天。
        return duration >= span_days
    return span_days > 1


def _daterange(start: date, end: date) -> list[str]:
    days = (end - start).days
    if days < 0:
        return []
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


def _normalize_period(raw: str) -> str:
    """时段归一化:「上午」/「下午」,空或未知值视为未填(默认整天)。"""
    text = raw.strip()
    if text in ("上午", "下午"):
        return text
    return ""


def _compute_coverage(start: date, end: date, start_period: str, end_period: str) -> tuple[list[str], list[str]]:
    """按日期+时段推算精确覆盖:返回 (整天的日期列表, 半天的日期列表)。

    语义:开始时段=上午 → 首日整天;下午 → 首日半天。结束时段=上午 →
    末日半天;下午 → 末日整天。时段未填按整天(兼容旧表)。
    时长(天)列不参与推算——它可能被填错,只做交叉校验。
    """
    if end < start:
        return [], []
    span = (end - start).days
    full: list[str] = []
    half: list[str] = []
    sp, ep = _normalize_period(start_period), _normalize_period(end_period)

    if span == 0:
        # 单日:起上午+止下午=整天;起上午+止上午=半天;起下午+止下午=半天;
        # 起下午+止上午=负区间,视为数据异常 → 半天兜底? 按 0 处理并全丢。
        if (sp == "" and ep == "") or (sp == "上午" and ep == "下午"):
            full.append(start.isoformat())
        elif (sp == "上午" and ep == "上午") or (sp == "下午" and ep == "下午"):
            half.append(start.isoformat())
        else:  # 下午起 + 上午止:异常区间,视为无效
            pass
        return full, half

    # 多天:首日 + 中间整天 + 末日
    if sp == "上午":
        full.append(start.isoformat())
    elif sp == "下午":
        half.append(start.isoformat())
    else:
        full.append(start.isoformat())
    for i in range(1, span):
        full.append((start + timedelta(days=i)).isoformat())
    if ep == "上午":
        half.append(end.isoformat())
    else:  # 下午 / 未填 → 整天
        full.append(end.isoformat())
    return full, half


async def _resolve_sheet_id(token: str, sheet_name: str) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve a worksheet tab title to its SHEET_ID. Returns (sheet_id, error_result)."""
    meta = await _core._invoke(_core._build_sheet_meta_request(token))
    if not meta["ok"]:
        return None, meta
    sheets = meta["data"].get("sheets", []) if isinstance(meta["data"], dict) else []
    target = sheet_name.strip()
    for sh in sheets if isinstance(sheets, list) else []:
        title = sh.get("title", "")
        if title == target:
            sheet_id = sh.get("sheet_id") or sh.get("sheetId")
            if sheet_id:
                return str(sheet_id), None
    available = [sh.get("title", "") for sh in sheets if isinstance(sh, dict)]
    return None, _core._error(f"No worksheet named {target!r} in this spreadsheet. Available: {available}")


async def query_leave_impl(
    sheet_token: str,
    sheet_name: str,
    date_from: str,
    date_to: str,
    names_json: str = "",
) -> dict[str, Any]:
    """Read the "请假表" sub-sheet and return each person's leave dates that overlap the window."""
    if not sheet_token.strip():
        return _core._error("sheet_token is required (same spreadsheet_token as the todo board).")
    if not sheet_name.strip():
        return _core._error("sheet_name is required — the worksheet tab title, e.g. '请假表'.")
    query_start = _parse_cell_date(date_from)
    query_end = _parse_cell_date(date_to)
    if query_start is None:
        return _core._error(f"date_from {date_from!r} is not a valid date (expected YYYY-MM-DD).")
    if query_end is None:
        return _core._error(f"date_to {date_to!r} is not a valid date (expected YYYY-MM-DD).")
    if query_end < query_start:
        return _core._error("date_to must not be before date_from.")

    names_filter: set[str] | None = None
    if names_json.strip():
        try:
            parsed = json.loads(names_json)
        except ValueError as exc:
            return _core._error(f"names_json is not valid JSON: {exc}")
        if not isinstance(parsed, list) or not all(isinstance(n, str) for n in parsed):
            return _core._error("names_json must be a JSON array of name strings.")
        # 空数组「[]」视为不过滤(查所有人)——空集合过滤器会静默跳过每一行,
        # 与省略该参数的语义一致才对。名单与单元格姓名都做 @ 前缀归一化。
        names = {_strip_mention(n) for n in parsed if _strip_mention(n)}
        if names:
            names_filter = names

    sheet_id, err = await _resolve_sheet_id(sheet_token.strip(), sheet_name)
    if err is not None:
        return err
    assert sheet_id is not None

    values_res = await _core._invoke(_core._build_sheet_values_request(sheet_token.strip(), sheet_id))
    if not values_res["ok"]:
        return values_res
    value_range = values_res["data"].get("valueRange", {}) if isinstance(values_res["data"], dict) else {}
    raw_rows = value_range.get("values") or []
    rows = [[_core._flatten_sheet_cell(c) for c in (row if isinstance(row, list) else [])] for row in raw_rows]
    if not rows:
        return {
            "ok": True,
            "date_from": query_start.isoformat(),
            "date_to": query_end.isoformat(),
            "results": [],
            "count": 0,
        }

    header, *data_rows = rows
    columns = _index_columns(header)
    missing = [f for f in ("name", "start", "end") if f not in columns]
    if missing:
        return _core._error(
            f"Could not find required column(s) {missing} in the header row {header!r}. "
            "Expected something like 姓名 / 开始日期 / 结束日期."
        )

    def cell(row: list[str], field: str) -> str:
        idx = columns.get(field)
        return row[idx] if idx is not None and idx < len(row) else ""

    results: list[dict[str, Any]] = []
    for row_num, row in enumerate(data_rows, start=2):  # header is row 1
        name = cell(row, "name").strip()
        if not name:
            continue
        if names_filter is not None and _strip_mention(name) not in names_filter:
            continue
        start = _parse_cell_date(cell(row, "start"))
        end = _parse_cell_date(cell(row, "end"))
        if start is None or end is None:
            continue
        overlap_start = max(start, query_start)
        overlap_end = min(end, query_end)
        if overlap_start > overlap_end:
            continue
        span_days = (end - start).days + 1
        start_period = cell(row, "start_period")
        end_period = cell(row, "end_period")
        full_days, half_days = _compute_coverage(start, end, start_period, end_period)
        computed_days = len(full_days) + 0.5 * len(half_days)
        duration_value = cell(row, "duration").strip()
        duration_parsed = _parse_duration(duration_value)
        duration_mismatch = duration_parsed is not None and abs(duration_parsed - computed_days) > 0.01
        results.append(
            {
                "row": row_num,
                "name": name,
                "leave_type": cell(row, "type").strip(),
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "start_period": _normalize_period(start_period),
                "end_period": _normalize_period(end_period),
                # 向后兼容:覆盖采集日且当天整天(旧 SKILL 用);新规则用 covered_* 判周期段。
                "is_full_day": _is_full_day(cell(row, "full_day"), cell(row, "duration"), span_days),
                # 按日期+时段推算的精确覆盖(权威);时长列仅交叉校验。
                "covered_full_days": full_days,
                "covered_half_days": half_days,
                "total_days": computed_days,
                "duration_value": duration_value,
                "duration_mismatch": duration_mismatch,
                "note": cell(row, "note").strip(),
                "overlap_start": overlap_start.isoformat(),
                "overlap_end": overlap_end.isoformat(),
                "hit_dates": _daterange(overlap_start, overlap_end),
            }
        )

    return {
        "ok": True,
        "date_from": query_start.isoformat(),
        "date_to": query_end.isoformat(),
        "results": results,
        "count": len(results),
    }
