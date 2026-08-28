# ruff: noqa: RUF002, RUF003  # 中文全角标点是刻意排版,非歧义字符
"""Mentor 报表卡发送工具（T4）—— 读本周期台账 → 统计 → 渲染 → 私聊发卡。

对接 ``company-todo-sync`` 第 5 节「报表只推给 mentor 本人」：每次采集/派发
周期跑完后，对每个 mentor 调本工具，把本周期表的行读回来，用
``_report_stats.build_mentor_stats``（T3 统计口径纯函数，唯一权威口径）算好
卡面文案，经 ``card-dsl`` 的 ``mentor-report-card`` 模板渲染成纯只读报表卡，
私聊发给 mentor 本人。

四条铁律（与 SKILL 一致，写代码时固化）：
- **统计必须现场读**：本工具每次调用都重新拉本周期表全部行再统计，禁止复用
  记忆里的数字或接受调用方传入的统计结果——发卡工具只信自己读回来的行。
- **口径唯一**：统计只走 ``build_mentor_stats``，这里不实现第二套计数。
- **只读卡**：渲染出的卡片不含任何按钮/回调（``handlers`` 恒为空），报表卡是
  「看」的，不是「点」的。
- **测试模式**：设置了 ``PSI_REPORT_CARD_TEST_RECEIVE_ID`` 或传了
  ``test_receive_id`` 时，卡片发给测试者本人代替 mentor，严禁发到真实 mentor
  手上（与评价卡 ``PSI_REVIEW_CARD_TEST_RECEIVE_ID`` 同一约定）。

读台账身份：``user_key`` 传入时 tenant 优先、被拒自动回退该用户身份
（``_core._invoke`` 默认行为）；mentor 台账 base 若已把应用加为协作者，
tenant 直读即可，无需任何个人 token。
"""

from __future__ import annotations

import datetime
import json
import os
from typing import Any

import _feishu_impl as _core
import _report_stats as _stats
from _card_dsl import render_template
from _feishu.bitable import _build_list_records_request

# 测试模式：设了该环境变量时,报表卡发给该 open_id(测试者本人)代替真实 mentor,
# 严禁把报表卡发到真实 mentor 手上。正式运行(海豚一号服务器)不设此变量 → 发真 mentor。
# 每次调用实时读取（不缓存模块级常量），部署时可热切换测试/正式。
_TEST_ENV_VAR = "PSI_REPORT_CARD_TEST_RECEIVE_ID"

_PAGE_SIZE = 500

# 逾期/请假在明细表里的标色（引擎对单元格文本按 markdown 放行）。
_STATUS_COLOR: dict[str, str] = {
    _stats.STATUS_OVERDUE: "red",
    _stats.STATUS_LEAVE: "orange",
}


def _cell_text(field_value: Any) -> str:
    """单元格扁平化为可见文本（人员/单选 dict → text/name，数组拼接）。"""
    if isinstance(field_value, str):
        return field_value
    if isinstance(field_value, dict):
        return str(field_value.get("text") or field_value.get("name") or field_value.get("id") or "")
    if isinstance(field_value, list):
        parts: list[str] = []
        for entry in field_value:
            if isinstance(entry, dict):
                parts.append(
                    str(entry.get("text") or entry.get("name") or entry.get("id") or "")
                )
            else:
                parts.append(str(entry))
        return "".join(parts)
    return str(field_value or "")


def _date_label(value: Any) -> str:
    """Bitable 日期字段（epoch 毫秒）→ "MM-DD"；空/坏值返回空串。"""
    if isinstance(value, (int, float)) and value:
        try:
            return datetime.datetime.fromtimestamp(int(value) / 1000).strftime("%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    return ""


def _status_label(status: Any) -> str:
    """状态列显示文本：逾期标红、请假标橙，其余原样。"""
    text = _cell_text(status)
    if not text:
        return ""
    color = _STATUS_COLOR.get(text)
    if color:
        return f"<font color='{color}'>{text}</font>"
    return text


def _person_name(field_value: Any) -> str:
    """人员字段取首个 name，用于明细分组展示；空返回空串。"""
    if isinstance(field_value, list):
        for entry in field_value:
            if isinstance(entry, dict) and entry.get("name"):
                return str(entry["name"])
    if isinstance(field_value, dict) and field_value.get("name"):
        return str(field_value["name"])
    return ""


_PERSON_FIELDS = frozenset({"负责人", "mentor"})


def _normalize_person_field(field_value: Any) -> Any:
    """把人员字段规整成统计核心认识的形状。

    Bitable 人员列读回是 ``[{"id": ..., "name": ...}]`` 数组，而
    ``build_mentor_stats``（T3 纯函数）的 ``_people_key`` 只处理 dict/字符串
    ——数组会被 ``str()`` 化，导致去重键变成一坨 JSON、未填判定永远失配。
    这里取第一个人员收成 ``{"id": ..., "name": ...}``：统计去重按 id、
    未填判定按 open_id，跨人重名不会合并。这是数据适配，不是另算统计。
    """
    if not isinstance(field_value, list):
        return field_value
    for entry in field_value:
        if isinstance(entry, dict):
            oid = entry.get("id") or entry.get("open_id") or entry.get("user_id")
            name = entry.get("name")
            if oid or name:
                return {"id": str(oid or ""), "name": str(name or "")}
    return None


def _stat_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """统计用行：人员字段规整，其余原样（统计核心只读这些键）。"""
    return {k: (_normalize_person_field(v) if k in _PERSON_FIELDS else v) for k, v in fields.items()}


async def _fetch_cycle_rows(
    app_token: str, table_id: str, user_key: str
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """分页读回本周期表全部行，返回 ([{record_id, fields}...], error_or_None)。"""
    rows: list[dict[str, Any]] = []
    page_token = ""
    while True:
        req = _build_list_records_request(
            app_token, table_id, _PAGE_SIZE, page_token, filter_="", sort="", field_names=""
        )
        res = await _core._invoke(req, user_key=user_key)
        if not res["ok"]:
            return rows, res
        data = res["data"] if isinstance(res["data"], dict) else {}
        items = data.get("items", []) if isinstance(data.get("items"), list) else []
        for item in items:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields", {})
            rows.append(
                {
                    "record_id": str(item.get("record_id") or ""),
                    "fields": fields if isinstance(fields, dict) else {},
                }
            )
        next_token = data.get("page_token", "") or ""
        if not data.get("has_more") or not next_token:
            return rows, None
        if next_token == page_token:
            # 防御：飞书偶发在 has_more=true 时原样回传同一 page_token，
            # 直接继续会死循环卡死整个 sync 流程；停滞即停止，宁缺勿挂。
            return rows, None
        page_token = next_token


def _table_rows(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """明细行展开：模板 table 的列字段 level/title/due/status/score。

    按负责人分组排列（同一人的条目集中一块），组内按层级类别
    （大目标 → 小目标 → todo → 其他）再按标签/标题稳定排序；
    与 SKILL「报表明细按负责人分组排列」一致。不能用字典序排层级——
    "todo1" 的 ASCII 码点比 "大目标1" 小,按字符串排序会把 todo 排到
    大目标前面,违背展示直觉。
    """
    kind_rank = {"big": 0, "small": 1, "todo": 2}
    rows: list[dict[str, str]] = []
    for rec in records:
        f = rec["fields"]
        level = _cell_text(f.get("层级"))
        rows.append(
            {
                "level": level,
                "title": _cell_text(f.get("标题")),
                "due": _date_label(f.get("截止日期")),
                "status": _status_label(f.get("状态")),
                "score": _cell_text(f.get("mentor打分")),
                "_owner": _person_name(f.get("负责人")),
                "_kind": kind_rank.get(_stats._goal_kind(level), 3),
            }
        )
    rows.sort(key=lambda r: (r["_owner"], r["_kind"], r["level"], r["title"]))
    for r in rows:
        r.pop("_owner", None)
        r.pop("_kind", None)
    return rows


def _resolve_expected_people(records: list[dict[str, Any]], expected_people: list[str]) -> list[str]:
    """把 expected_people 归一化成与统计核心一致的负责人键（姓名）。

    ``build_mentor_stats`` 的未填判定键是 ``_row_field(负责人)`` 取的人员
    **name**（人员 dict 的 name 优先于 id），所以名单条目直接传姓名即可
    匹配；若调用方传的是 open_id，这里转成对应行的 name 再比较。未命中
    任何行的条目原样保留（可能是确实没填报的人）。
    """
    names: set[str] = set()
    oid_to_name: dict[str, str] = {}
    for rec in records:
        for entry in rec["fields"].get("负责人") or []:
            if not isinstance(entry, dict):
                continue
            nm = entry.get("name")
            oid = entry.get("id") or entry.get("open_id") or entry.get("user_id")
            if nm:
                names.add(str(nm))
            if nm and oid:
                oid_to_name.setdefault(str(oid), str(nm))
    out: list[str] = []
    for p in expected_people:
        key = str(p)
        if key in names:
            out.append(key)
        elif key in oid_to_name:
            out.append(oid_to_name[key])
        else:
            out.append(key)
    return out


async def feishu_mentor_report_send(
    mentor_open_id: str = "",
    mentor_name: str = "",
    cycle_date: str = "",
    ledger_app_token: str = "",
    ledger_table_id: str = "",
    expected_people_json: str = "",
    trend: str = "",
    test_receive_id: str = "",
    user_key: str = "",
) -> str:
    """给一位 mentor 私聊发送本周期 TODO 报表卡（纯只读）。

    Args:
        mentor_open_id: 收卡 mentor 的 open_id（必填）。
        mentor_name: 团队名（卡头显示 "<mentor_name>团队"）（必填）。
        cycle_date: 周期日期 YYYY-MM-DD（必填，卡头 "MM-DD" 与台账链接）。
        ledger_app_token: 该 mentor 台账 base 的 app_token（必填）。
        ledger_table_id: **本周期表**的 table_id（必填；由
            ``feishu_mentor_ledger_cycle_table`` 拿到，不是历史表）。
        expected_people_json: 可选 JSON 数组——本周期应填报的负责人名单
            （姓名/open_id 字符串），用于「应填未填」判定取红卡头。
        trend: 可选——完成率趋势文案（如 "61%→68%→72%（近6周期）"），
            数据不足时留空，模板渲染为 "—"。
        test_receive_id: 测试模式收卡人 open_id，优先于环境变量
            ``PSI_REPORT_CARD_TEST_RECEIVE_ID``；二者皆空时发真实 mentor。
        user_key: 调用者 open_id（读台账身份回退用）。

    Returns:
        JSON 字符串：ok / mentor_open_id / receive_id / test_override /
        row_count / counts（统计结构化结果）/ message_id（发送成功时）。
    """
    mentor_open_id = mentor_open_id.strip()
    mentor_name = mentor_name.strip()
    cycle_date = cycle_date.strip()
    app_token = ledger_app_token.strip()
    table_id = ledger_table_id.strip()
    if not mentor_open_id:
        return json.dumps({"ok": False, "error": "mentor_open_id is required"}, ensure_ascii=False)
    if not mentor_name:
        return json.dumps({"ok": False, "error": "mentor_name is required"}, ensure_ascii=False)
    if not cycle_date:
        return json.dumps({"ok": False, "error": "cycle_date is required (YYYY-MM-DD)"}, ensure_ascii=False)
    if not app_token:
        return json.dumps({"ok": False, "error": "ledger_app_token is required"}, ensure_ascii=False)
    if not table_id:
        return json.dumps({"ok": False, "error": "ledger_table_id is required (the cycle's table)"}, ensure_ascii=False)

    expected_people: list[str] = []
    if expected_people_json.strip():
        try:
            parsed = json.loads(expected_people_json)
        except ValueError as exc:
            err = f"expected_people_json is not valid JSON: {exc}"
            return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        if not isinstance(parsed, list):
            return json.dumps({"ok": False, "error": "expected_people_json must be a JSON array"}, ensure_ascii=False)
        expected_people = [str(p) for p in parsed]

    # ── 1. 现场读本周期表全部行 ────────────────────────────────────────────
    records, err = await _fetch_cycle_rows(app_token, table_id, user_key)
    if err is not None:
        return json.dumps(
            {"ok": False, "error": err.get("message") or err.get("error") or "ledger read failed"},
            ensure_ascii=False,
            default=str,
        )

    # ── 2. 统计（口径唯一：build_mentor_stats）─────────────────────────────
    stat_rows = [_stat_fields(r["fields"]) for r in records]
    resolved_expected = _resolve_expected_people(records, expected_people)
    stats = _stats.build_mentor_stats(stat_rows, expected_people=resolved_expected)

    # ── 3. 渲染模板（mentor-report-card，纯只读）──────────────────────────
    rendered = render_template(
        "mentor-report-card",
        values_json=json.dumps(
            {
                "template": stats["template"],
                "cycle_label": cycle_date[5:] if len(cycle_date) >= 10 else cycle_date,
                "mentor_name": mentor_name,
                "people_summary": stats["people_summary"],
                "goal_summary": stats["goal_summary"],
                "done_summary": stats["done_summary"],
                "score_summary": stats["score_summary"],
                "trend": trend.strip() or "—",
                "ledger_url": f"https://genuineknowledge.feishu.cn/base/{app_token}?table={table_id}",
            },
            ensure_ascii=False,
        ),
        context_json=json.dumps({"rows": _table_rows(records)}, ensure_ascii=False),
    )
    if not rendered.get("ok"):
        return json.dumps(
            {"ok": False, "error": rendered.get("error") or "dsl render failed"},
            ensure_ascii=False,
        )
    card, handlers = rendered["card"], rendered["handlers"]
    if handlers:
        # 报表卡是纯只读的,模板编译不该产出任何回调;若未来模板加了按钮,
        # 这里要同步补 business_context 与映射,而不是静默发一张会出事的卡。
        return json.dumps({"ok": False, "error": "mentor report card must be read-only"}, ensure_ascii=False)

    # ── 4. 私聊发卡（测试模式覆盖收卡人）─────────────────────────────────
    env_test = os.environ.get(_TEST_ENV_VAR, "").strip()
    receive_id = (test_receive_id.strip() or env_test or mentor_open_id).strip()
    test_override = receive_id != mentor_open_id
    business_context = {
        "kind": "company_todo_mentor_report",
        "mentor_open_id": mentor_open_id,
        "mentor_name": mentor_name,
        "cycle_date": cycle_date,
        "ledger_app_token": app_token,
        "ledger_table_id": table_id,
    }
    try:
        res = await _core.send_card_impl(
            receive_id=receive_id,
            card_json=json.dumps(card, ensure_ascii=False),
            receive_id_type="open_id",
            user_key=user_key,
            business_context_json=json.dumps(business_context, ensure_ascii=False),
            action_handlers_json="{}",
        )
    except Exception as e:
        return json.dumps({"ok": False, "error": f"{e!r}"}, ensure_ascii=False)
    if not res.get("ok"):
        return json.dumps(
            {"ok": False, "error": res.get("message") or res.get("error") or res},
            ensure_ascii=False,
            default=str,
        )

    result: dict[str, Any] = {
        "ok": True,
        "mentor_open_id": mentor_open_id,
        "receive_id": receive_id,
        "test_override": test_override,
        "row_count": len(records),
        "counts": stats["counts"],
    }
    message_id = res.get("message_id")
    if isinstance(message_id, str) and message_id:
        result["message_id"] = message_id
    return json.dumps(result, ensure_ascii=False, default=str)
