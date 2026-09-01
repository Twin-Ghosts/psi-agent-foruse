#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六区数字卡 v9（2026-08-29 boss 总览卡加灰底数字格）。

v8 信息架构不动（无表格、链接化、六区数字格、8 字趋势），v9 在 boss 总览卡
顶部新增三行与 mentor 卡同构的灰底数字格（background_style=grey）：

    👥 ① 人员与填报   填报 30/31 · 在册 31 · 未按时 0
    📊 ② 任务与台账   已闭环 · 进行中 · 逾期
    🏢 ③ 团队与考勤   团队 · 请假 · 考勤异常

数字 26px 语义色（绿 #34C724 / 蓝 #3370FF / 橙 #FA8C16 / 红 #F53F3F），
让 boss 不点开明细表也能直接从卡片拿到关键数字；下方仍保留各明细表链接。

产出：
    mentor_cards/mentor_cards_v9.json   全部卡 {mentor: {oid, card}, __boss__: {card}}
    mentor_cards/data/trend_data.json   各组+boss 趋势序列（供海豚生成折线图）
    六区数字卡-真实卡片v9.json         孙逊组卡（验收）
    六区数字卡-真实卡片v9-boss.json    boss 总览卡（验收）
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

WS = Path(__file__).resolve().parent
sys.path.insert(0, str(WS / "mentor-cards"))
import build_cards as B  # noqa: E402

FEISHU_DOC_BASE = "https://genuineknowledge.feishu.cn"  # 租户域名（docx 创建接口不返回 url，按此拼接）
TODO_LIST_URL = f"{FEISHU_DOC_BASE}/wiki/H6icwLWn1iwpXAk73QMcA6MgnWc"
DETAIL_BASES = WS / "mentor-cards" / "detail_bases.json"
TREND_DATA = WS / "mentor-cards" / "data" / "trend_data.json"

# 协作者（自建明细表/趋势文档加协作者用；幂等，失败忽略）
COLLABORATORS = [
    {"open_id": "ou_4252f65b5d15191a793262f318c1f598", "name": "马晨柯"},
    {"open_id": "ou_0c56129dd1574d659af087c88cfe626e", "name": "孙逊"},
]

# 趋势口径库：全部 ≤8 字；trend_desc 按真实趋势实时选择
TREND_LEXICON = {
    "full": "持续满员",      # 近 N 期填报率全部 100%
    "high": "高位稳定",      # 均值≥90 且极差≤8
    "up": "稳步向好",        # 后半程明显高于前半程且最新≥95
    "up_slow": "稳步上升",   # 后半程明显高于前半程（最新<95）
    "down": "连续下滑",      # 后半程明显低于前半程且最新≤85
    "down_slow": "有所回落", # 后半程明显低于前半程（最新>85）
    "wave": "波动明显",      # 极差≥15 且无单调方向
    "near": "接近满员",      # 最新≥98 但非全部满员
    "flat": "大体平稳",      # 其余
}


# ---------------------------------------------------------------------------
# 飞书 API 封装（tenant token；明细表建表/刷新用）
# ---------------------------------------------------------------------------
BASE = "https://open.feishu.cn"


def _req(method: str, path: str, body: dict | None = None, token: str = "") -> dict:
    import urllib.request
    url = BASE + path
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.loads(resp.read().decode("utf-8"))


def tenant_token() -> str:
    import urllib.request
    app_id = os.environ.get("PSI_FEISHU_APP_ID", "")
    app_secret = os.environ.get("PSI_FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        print("[err] 缺少 PSI_FEISHU_APP_ID / PSI_FEISHU_APP_SECRET", file=sys.stderr)
        sys.exit(1)
    res = _req("POST", "/open-apis/auth/v3/tenant_access_token/internal",
               {"app_id": app_id, "app_secret": app_secret})
    if res.get("code") != 0:
        print(f"[err] 拿 token 失败: {res}", file=sys.stderr)
        sys.exit(1)
    return res["tenant_access_token"]


# ---------------------------------------------------------------------------
# 明细表注册（detail_bases.json）——自建表，数据驱动挂载，幂等
# ---------------------------------------------------------------------------
def _load_detail_bases() -> dict:
    if DETAIL_BASES.exists():
        try:
            return json.loads(DETAIL_BASES.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    return {}


def _save_detail_bases(db: dict) -> None:
    DETAIL_BASES.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")


def _list_record_ids(token: str, app: str, table: str) -> list[str]:
    import urllib.parse
    ids: list[str] = []
    page_token = ""
    while True:
        q = "page_size=500" + (f"&page_token={urllib.parse.quote(page_token)}" if page_token else "")
        res = _req("GET", f"/open-apis/bitable/v1/apps/{app}/tables/{table}/records?{q}", token=token)
        if res.get("code") != 0:
            raise RuntimeError(f"读表记录失败: {res}")
        data = res.get("data") or {}
        ids += [r["record_id"] for r in (data.get("items") or [])]
        if data.get("has_more") and data.get("page_token"):
            page_token = data["page_token"]
        else:
            break
    return ids


def _replace_records(token: str, app: str, table: str, fields_list: list[dict]) -> None:
    ids = _list_record_ids(token, app, table)
    for i in range(0, len(ids), 500):
        _req("POST", f"/open-apis/bitable/v1/apps/{app}/tables/{table}/records/batch_delete",
             {"records": ids[i:i + 500]}, token)
    for i in range(0, len(fields_list), 500):
        recs = [{"fields": f} for f in fields_list[i:i + 500]]
        res = _req("POST", f"/open-apis/bitable/v1/apps/{app}/tables/{table}/records/batch_create",
                   {"records": recs}, token)
        if res.get("code") != 0:
            raise RuntimeError(f"写记录失败: {res}")
    print(f"    ↳ {table}: 清空后写入 {len(fields_list)} 条")


def _list_tables(token: str, app: str) -> dict[str, str]:
    """app 下全部表 {表名: table_id}。"""
    res = _req("GET", f"/open-apis/bitable/v1/apps/{app}/tables?page_size=100", token=token)
    if res.get("code") != 0:
        raise RuntimeError(f"列表失败: {res}")
    return {t["name"]: t["table_id"] for t in (res.get("data") or {}).get("items", [])}


def _ensure_table(token: str, app: str, name: str, fields: list[dict]) -> str:
    """按表名幂等建表（已存在直接返回 table_id）。fields 首位必须是文本/数字类索引列。"""
    tables = _list_tables(token, app)
    if name in tables:
        return tables[name]
    res = _req("POST", f"/open-apis/bitable/v1/apps/{app}/tables",
               {"table": {"name": name, "fields": fields, "default_view_name": "表格"}}, token)
    if res.get("code") != 0:
        raise RuntimeError(f"建表失败 {name}: {res}")
    return res["data"]["table_id"]


def _ensure_base(token: str, name: str) -> str:
    """按名幂等建 base（按 app 名不可查重，由调用方保证用 detail_bases 注册避免重复）。"""
    res = _req("POST", "/open-apis/bitable/v1/apps", {"name": name}, token)
    if res.get("code") != 0:
        raise RuntimeError(f"建 base 失败 {name}: {res}")
    return res["data"]["app"]["app_token"]


def _add_collaborators(token: str, app_token: str) -> None:
    """给自建 base 加协作者（edit 权限，幂等；失败忽略不阻断）。"""
    for c in COLLABORATORS:
        try:
            _req("POST",
                 f"/open-apis/drive/v1/permissions/{app_token}/members?type=bitable",
                 {"member_type": "openid", "member_id": c["open_id"],
                  "perm": "edit", "type": "user"}, token)
        except Exception as exc:  # noqa: BLE001
            print(f"    ↳ 加协作者 {c['name']} 失败（忽略）: {exc}")


_TASK_FIELDS = [
    {"field_name": "负责人", "type": 1},
    {"field_name": "任务", "type": 1},
    {"field_name": "截止", "type": 1},
]
_TEAM_FIELDS = [
    {"field_name": "团队", "type": 1},
    {"field_name": "人数", "type": 2},
    {"field_name": "填报", "type": 2},
    {"field_name": "未填", "type": 2},
    {"field_name": "请假", "type": 2},
    {"field_name": "逾期", "type": 2},
    {"field_name": "填报率", "type": 1},
]
_BOSS_TASK_FIELDS = [{"field_name": "团队", "type": 1}] + _TASK_FIELDS

# v7：boss 卡新增四张明细表（台账总览 / 逾期明细 / 请假标注 / 考勤异常）
_BOSS_LEDGER_FIELDS = [
    {"field_name": "团队", "type": 1},
    {"field_name": "大", "type": 2},
    {"field_name": "小", "type": 2},
    {"field_name": "TODO", "type": 2},
    {"field_name": "已闭环", "type": 2},
    {"field_name": "进行中", "type": 2},
    {"field_name": "逾期", "type": 2},
    {"field_name": "评分", "type": 2},
    {"field_name": "评数", "type": 2},
    {"field_name": "来源", "type": 1},
    {"field_name": "台账", "type": 15},
]
_BOSS_OVERDUE_FIELDS = [
    {"field_name": "团队", "type": 1},
    {"field_name": "负责人", "type": 1},
    {"field_name": "任务", "type": 1},
    {"field_name": "标记", "type": 1},
]
_BOSS_LEAVE_FIELDS = [
    {"field_name": "姓名", "type": 1},
    {"field_name": "团队", "type": 1},
    {"field_name": "类型", "type": 1},
    {"field_name": "开始", "type": 1},
    {"field_name": "结束", "type": 1},
    {"field_name": "天数", "type": 1},
    {"field_name": "状态", "type": 1},
    {"field_name": "事由", "type": 1},
]
_BOSS_ATT_FIELDS = [
    {"field_name": "姓名", "type": 1},
    {"field_name": "异常", "type": 1},
    {"field_name": "统计窗口", "type": 1},
]


def _task_row(owner: str, title: str, due: str) -> dict:
    return {"负责人": owner, "任务": title, "截止": due}


def ensure_mentor_task_tables(mentor: str, closed_rows: list[list[str]],
                              doing_rows: list[list[str]]) -> None:
    """确保该 mentor 的「已闭环任务 / 进行中任务」多维表格存在并写入最新数据。

    - 无台账（rows 为空）→ 不建表，返回。
    - 已注册（detail_bases.json[mentor].tasks）→ 复用 base 刷新。
    - 孙逊已有 base → 复用之；其余组按需新建「海豚·XX组·周期明细」。
    结果注册/刷新 detail_bases.json[mentor].tasks。
    """
    if not closed_rows and not doing_rows:
        return
    db = _load_detail_bases()
    reg = db.setdefault(mentor, {})
    token = tenant_token()
    app = reg.get("app_token") or ""
    if not app:
        app = _ensure_base(token, f"海豚·{mentor}组·周期明细（任务）")
        reg["app_token"] = app
        reg["name"] = f"海豚·{mentor}组·周期明细（任务）"
        _add_collaborators(token, app)
        print(f"[明细表] {mentor} 新建 base {app}")
    tasks = reg.setdefault("tasks", {})
    if closed_rows:
        tid = _ensure_table(token, app, "已闭环任务", _TASK_FIELDS)
        url = f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"
        tasks["closed"] = {"table_id": tid, "url": url}
        _replace_records(token, app, tid,
                         [_task_row(*r) for r in closed_rows])
    if doing_rows:
        tid = _ensure_table(token, app, "进行中任务", _TASK_FIELDS)
        url = f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"
        tasks["doing"] = {"table_id": tid, "url": url}
        _replace_records(token, app, tid,
                         [_task_row(*r) for r in doing_rows])
    if not tasks.get("closed") and not tasks.get("doing"):
        reg.pop("tasks", None)
    _save_detail_bases(db)


def ensure_boss_tables(team_rows: list[list[str]], all_closed: list[list[str]],
                       all_doing: list[list[str]], ledger_rows: list[dict],
                       all_overdue: list[tuple[str, str, str, str]],
                       g_leaves: list[dict],
                       anomalies: dict[str, list[str]], att_window: str,
                       name_to_mentor: dict[str, str]) -> None:
    """确保 boss「海豚·全公司·周期明细」base 存在并写入。

    v7 表集：团队维度 / 已闭环任务 / 进行中任务 / 台账总览 / 逾期明细 / 请假标注 / 考勤异常。
    全部按表名幂等：已存在则整表刷新记录，注册 url 到 detail_bases.json[__boss__]。
    """
    db = _load_detail_bases()
    reg = db.setdefault("__boss__", {})
    token = tenant_token()
    app = reg.get("app_token") or ""
    if not app:
        app = _ensure_base(token, "海豚·全公司·周期明细")
        reg["app_token"] = app
        reg["name"] = "海豚·全公司·周期明细"
        _add_collaborators(token, app)
        print(f"[明细表] boss 新建 base {app}")
    # 团队维度
    tid = _ensure_table(token, app, "团队维度", _TEAM_FIELDS)
    reg["team"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
    _replace_records(token, app, tid,
                     [{"团队": r[0], "人数": int(r[1] or 0), "填报": int(r[2] or 0),
                       "未填": int(r[3] or 0), "请假": int(r[4] or 0), "逾期": int(r[5] or 0),
                       "填报率": r[6]} for r in team_rows])
    # 任务两表
    tasks = reg.setdefault("tasks", {})
    if all_closed:
        tid = _ensure_table(token, app, "已闭环任务", _BOSS_TASK_FIELDS)
        tasks["closed"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
        _replace_records(token, app, tid,
                         [{"团队": r[0], "负责人": r[1], "任务": r[2], "截止": r[3]}
                          for r in all_closed])
    if all_doing:
        tid = _ensure_table(token, app, "进行中任务", _BOSS_TASK_FIELDS)
        tasks["doing"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
        _replace_records(token, app, tid,
                         [{"团队": r[0], "负责人": r[1], "任务": r[2], "截止": r[3]}
                          for r in all_doing])
    # v7：台账总览
    tid = _ensure_table(token, app, "台账总览", _BOSS_LEDGER_FIELDS)
    reg["ledger"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
    _replace_records(token, app, tid, ledger_rows)
    # v7：逾期明细
    if all_overdue:
        tid = _ensure_table(token, app, "逾期明细", _BOSS_OVERDUE_FIELDS)
        reg["overdue"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
        _replace_records(token, app, tid,
                         [{"团队": g, "负责人": o, "任务": t, "标记": m}
                          for g, o, t, m in all_overdue])
    # v7：请假标注
    if g_leaves:
        tid = _ensure_table(token, app, "请假标注", _BOSS_LEAVE_FIELDS)
        reg["leave"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
        _replace_records(token, app, tid,
                         [{"姓名": e["name"], "团队": name_to_mentor.get(e["name"], ""),
                           "类型": e["types"],
                           "开始": (e.get("start") or "")[5:10],
                           "结束": (e.get("end") or "")[5:10],
                           "天数": f"{e['days']:g}" if e.get("days") else "",
                           "状态": "✅已批准",
                           "事由": e.get("reason") or ""}
                          for e in g_leaves])
    # v7：考勤异常
    if anomalies:
        tid = _ensure_table(token, app, "考勤异常", _BOSS_ATT_FIELDS)
        reg["att"] = {"table_id": tid, "url": f"{FEISHU_DOC_BASE}/base/{app}?table={tid}"}
        _replace_records(token, app, tid,
                         [{"姓名": n, "异常": "、".join(it), "统计窗口": att_window}
                          for n, it in sorted(anomalies.items())])
    _save_detail_bases(db)


def refresh_registered_tables(mentor: str, g_leaves: list[dict],
                              overdue: list[tuple[str, str, str]],
                              anomalies: dict[str, list[str]], att_window: str) -> None:
    """若该 mentor 已在 detail_bases.json 注册请假/逾期/考勤表 → 刷新记录（幂等）。"""
    db = _load_detail_bases()
    reg = db.get(mentor)
    if not (reg and reg.get("app_token")):
        return
    token = tenant_token()
    app = reg["app_token"]
    print(f"[明细表] {mentor} 复用已注册 base {app}（{reg.get('name')}）")
    if reg.get("leave"):
        leave_rows = [
            {"姓名": e["name"], "类型": e["types"],
             "开始": (e.get("start") or "")[5:10], "结束": (e.get("end") or "")[5:10],
             "天数": f"{e['days']:g}" if e.get("days") else "",
             "事由": e.get("reason") or ""}
            for e in g_leaves]
        _replace_records(token, app, reg["leave"]["table_id"], leave_rows)
    if reg.get("overdue"):
        overdue_rows = [{"负责人": o, "任务": t, "标记": m} for o, t, m in overdue]
        _replace_records(token, app, reg["overdue"]["table_id"], overdue_rows)
    if reg.get("att"):
        att_rows = [{"姓名": n, "异常": it, "统计窗口": att_window}
                    for n, items in sorted(anomalies.items()) for it in items]
        _replace_records(token, app, reg["att"]["table_id"], att_rows)


def mentor_links(mentor: str) -> tuple[str, str, str]:
    """该 mentor 的自建明细链接（leave_url, overdue_url, att_url）；未注册返回空串。"""
    reg = (_load_detail_bases().get(mentor) or {})
    get = lambda key: (reg.get(key) or {}).get("url", "") if reg.get(key) else ""
    return get("leave"), get("overdue"), get("att")


def mentor_task_links(mentor: str) -> tuple[str, str]:
    """(已闭环任务 url, 进行中任务 url)；未注册返回空串。"""
    tasks = (_load_detail_bases().get(mentor) or {}).get("tasks") or {}
    return (tasks.get("closed") or {}).get("url", ""), (tasks.get("doing") or {}).get("url", "")


def boss_links() -> dict[str, str]:
    """boss 全部明细表 url：team/ledger/overdue/leave/att/closed/doing；未注册返回空串。"""
    reg = (_load_detail_bases().get("__boss__") or {})
    out: dict[str, str] = {}
    for key in ("team", "ledger", "overdue", "leave", "att"):
        out[key] = (reg.get(key) or {}).get("url", "")
    tasks = reg.get("tasks") or {}
    out["closed"] = (tasks.get("closed") or {}).get("url", "")
    out["doing"] = (tasks.get("doing") or {}).get("url", "")
    return out


def trend_url_of(reg_key: str) -> str:
    """reg_key 对应的趋势折线图 docx url（mentor 名或 '__boss__'）。"""
    reg = (_load_detail_bases().get(reg_key) or {})
    td = reg.get("trend") or {}
    url = td.get("url") or ""
    if not url and td.get("document_id"):
        url = f"{FEISHU_DOC_BASE}/docx/{td['document_id']}"
    return url


# ---------------------------------------------------------------------------
# 趋势：8 字口径选择（非请假口径；在假仍填报不算分子，集合差）
# ---------------------------------------------------------------------------
def trend_desc(series: list[tuple[str, int, int, int]]) -> str:
    """近 N 期 → ≤8 字趋势描述。

    series 元素 = (列名, 非请假填报人数, 非请假应填人数, 在假人数)，
    非请假填报 = |已填集合 − 在假集合|，由主流程算好传入。
    """
    rates: list[float] = []
    for _c, fl_nl, valid, _lv in series:
        rates.append(fl_nl / valid * 100 if valid > 0 else 0.0)
    if not rates:
        return "暂无数据"
    n = len(rates)
    r = rates[-1]
    avg = sum(rates) / n
    lo, hi = min(rates), max(rates)
    half = max(1, n // 2)
    first = sum(rates[:half]) / half
    last = sum(rates[half:]) / (n - half)
    slope = last - first
    if all(x >= 100 - 1e-9 for x in rates):
        return TREND_LEXICON["full"]
    if avg >= 90 and hi - lo <= 8:
        return TREND_LEXICON["high"]
    if slope >= 6:
        return TREND_LEXICON["up"] if r >= 95 else TREND_LEXICON["up_slow"]
    if slope <= -6:
        return TREND_LEXICON["down"] if r <= 85 else TREND_LEXICON["down_slow"]
    if hi - lo >= 15:
        return TREND_LEXICON["wave"]
    if r >= 98:
        return TREND_LEXICON["near"]
    return TREND_LEXICON["flat"]


# ---------------------------------------------------------------------------
# 卡片 2.0 组件（v6：无 table，全部用数字格 + 状态行 + 链接）
# ---------------------------------------------------------------------------
_TCOLOR = {"green": "#34C724", "blue": "#3370FF", "orange": "#FA8C16",
           "red": "#F53F3F", "default": "default"}
_NUM_FONT = "26px"


def tile(n: str, lab: str, color: str = "default") -> dict:
    """一个数字格：灰底、大号着色加粗数字 + 灰标签。"""
    c = _TCOLOR.get(color, "default")
    num = f"<font size='{_NUM_FONT}' color='{c}'>**{n}**</font>" if c != "default" \
        else f"<font size='{_NUM_FONT}'>**{n}**</font>"
    return {
        "tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
        "elements": [
            {"tag": "markdown", "content": num, "text_align": "center"},
            {"tag": "markdown", "content": f"<font color='grey'>{lab}</font>",
             "text_align": "center"},
        ],
    }


def tile_row3(*tiles: dict) -> dict:
    """三个数字格一行（灰底）。"""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "horizontal_spacing": "4px",
        "background_style": "grey",
        "columns": list(tiles),
    }


def sec(title: str, src: str = "") -> dict:
    """分区小标题（深色加粗 + 右侧来源灰字）。"""
    return {"tag": "markdown",
            "content": f"<font color='#1F2329'>**{title}**</font>"
                       + (f"<font color='#B0B6BF'>（{src}）</font>" if src else "")}


def lamp(icon: str, text: str, link: tuple[str, str] | None = None) -> dict:
    """状态行：emoji 图标 + 加粗文字 + 可选灰色链接。"""
    content = f"{icon} **{text}**"
    if link:
        content += f"　<font color='grey'>[{link[0]} →]({link[1]})</font>"
    return {"tag": "markdown", "content": content}


def link_line(label: str, url: str, n: int | None = None) -> dict:
    """「✅ 已闭环任务（2）　[明细 →](url)」式链接行。"""
    count = f"（{n}）" if n is not None else ""
    return {"tag": "markdown",
            "content": f"{label}{count}　<font color='grey'>[明细 →]({url})</font>"}


def esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def task_rows(rows: list[dict], status: str, with_group: bool = False,
              group: str = "") -> list[list[str]]:
    """从台账行提取某状态的任务行：[负责人, 任务(截断), 截止]；with_group 时前置团队名。"""
    out: list[list[str]] = []
    for r in rows:
        if (r.get("status") or "").strip() != status:
            continue
        owner = (r.get("owners") or "未指定").strip() or "未指定"
        title = (r.get("title") or "").strip()
        if not title:
            continue
        title = title if len(title) <= 22 else title[:22] + "…"
        due = (r.get("due") or "")
        due = due[5:] if len(due) >= 10 else due
        row = [owner, title, due]
        if with_group:
            row = [group] + row
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# 指标计算（复用 build_cards 数据通道，全部实时推导）
# ---------------------------------------------------------------------------
def compute_mentor_metrics(mentor: str, members: list[dict], latest: str,
                           date_cols: list[str], exempt: set[str],
                           join_map: dict[str, str]):
    cd = B._cycle_date(latest)
    leave_window, att_window = B.runtime_windows(latest)
    as_of = B.data_as_of()

    total = len(members)
    filled = sum(1 for p in members if B.member_status(p, latest, exempt, join_map, cd) == "filled")
    unfilled = sum(1 for p in members if B.member_status(p, latest, exempt, join_map, cd) == "unfilled")

    # 台账（②③⑥ + 逾期 + 任务表）
    ledger = B.load_ledger(mentor)
    rows = B.ledger_latest_rows(ledger)
    goals = B.goal_counts_from_ledger(rows) or (0, 0, 0)
    closure = B.closure_from_ledger(rows) or (0, 0, 0, 0, 0)
    eval_ = B.evaluation_from_ledger(rows) or {}
    if rows:
        ledger_src = f"台账·截至{(ledger.get('latest_cycle') or '')[-5:]}"
    else:
        cal = B.load_calibration()
        ledger_src = f"人工核对 {cal.get('calibrated_at', '')[:10]}"
    ledger_url = B.load_ledger_sources().get(mentor, {}).get("url", "")

    overdue = B._visible_overdue(
        B.overdue_from_ledger(rows) + B.auto_overdue_extra(
            members, latest, B.overdue_from_ledger(rows), exempt, join_map), exempt)
    overdue_cnt = len(overdue)

    g_leaves = B.group_leaves([p["name"] for p in members], leave_window)
    anomalies = B.attendance_anomalies({p["name"] for p in members})

    # 趋势（非请假口径；在假仍填报不算分子，集合差）
    approvals = B.approved_leaves()

    def on_leave(name: str, d: str) -> bool:
        return any((e.get("start") or "")[:10] <= d <= (e.get("end") or "")[:10]
                   for e in approvals if e.get("name") == name)

    ts = []  # (列名, 非请假填报人数, 非请假应填人数, 在假人数)
    for c in date_cols[-B.TREND_WINDOW:]:
        d = B._cycle_date(c)
        elig = [p for p in members if B.joined_by(join_map, p["name"], d)]
        lv = {p["name"] for p in elig if on_leave(p["name"], d)}
        fl = {p["name"] for p in elig if B._real_fill(p["cols"].get(c) or "")}
        ts.append((c, len(fl - lv), len(elig) - len(lv), len(lv)))
    desc = trend_desc(ts)

    # 当前期非请假口径填报率
    elig_cur = [p for p in members if B.joined_by(join_map, p["name"], cd)]
    lv_cur = {p["name"] for p in elig_cur if on_leave(p["name"], cd)}
    fl_cur = {p["name"] for p in elig_cur if B._real_fill(p["cols"].get(latest) or "")}
    valid_cur = len(elig_cur) - len(lv_cur)
    cur_pct = len(fl_cur - lv_cur) / valid_cur * 100 if valid_cur else 0.0

    return {
        "total": total, "filled": filled, "unfilled": unfilled,
        "goals": goals, "closure": closure, "eval": eval_,
        "ledger_src": ledger_src, "ledger_url": ledger_url,
        "overdue": overdue, "overdue_cnt": overdue_cnt,
        "g_leaves": g_leaves, "anomalies": anomalies,
        "ts": ts, "desc": desc, "cur_pct": cur_pct,
        "rows": rows, "att_window": att_window, "as_of": as_of,
    }


def build_mentor_v6(mentor: str, members: list[dict], latest: str,
                    date_cols: list[str], exempt: set[str],
                    join_map: dict[str, str]) -> dict:
    m = compute_mentor_metrics(mentor, members, latest, date_cols, exempt, join_map)
    cycle_no = len(date_cols)
    total, filled, unfilled = m["total"], m["filled"], m["unfilled"]
    goals, closure, eval_ = m["goals"], m["closure"], m["eval"]
    overdue_cnt, g_leaves, anomalies = m["overdue_cnt"], m["g_leaves"], m["anomalies"]
    ts, desc, cur_pct = m["ts"], m["desc"], m["cur_pct"]
    rows, ledger_url, ledger_src = m["rows"], m["ledger_url"], m["ledger_src"]

    lv_url, od_url, _att_url = mentor_links(mentor)
    closed_url, doing_url = mentor_task_links(mentor)
    trend_url = trend_url_of(mentor)

    att_color = "red" if anomalies else "green"
    _ATT_HEX = {"red": "#F53F3F", "green": "#34C724"}
    att_md = {"tag": "markdown", "content": (
        f"<font color='{_ATT_HEX.get(att_color, att_color)}'>⏰ 考勤异常（{m['att_window']}）：</font>"
        + ("、".join(f"{n}（{' · '.join(v)}）" for n, v in sorted(anomalies.items()))
           + (f"　<font color='grey'>[异常明细 →]({_att_url})</font>" if _att_url else "")
           if anomalies else "<font color='grey'>无</font>"))}

    dist = eval_.get("dist", {})
    bucket = " ｜ ".join(f"{lab}×{n}" for lab, n in
                         (("5★", dist.get("5", 0)), ("4★", dist.get("4", 0)),
                          ("3★", dist.get("3", 0)), ("≤2★", dist.get("le2", 0))) if n)

    # ✅ 已闭环 / 🔄 进行中 任务（台账实时；卡片只放链接，明细在多维表格）
    closed_rows = task_rows(rows, "已交付")
    doing_rows = task_rows(rows, "进行中")
    ledger_on = bool(rows)
    task_section: list[dict] = []
    if not ledger_on:
        task_section.append({"tag": "markdown",
                             "content": "<font color='grey'>（台账未启用，任务明细以台账为准）</font>"})
    else:
        if closed_rows:
            task_section.append(link_line("✅ 已闭环任务", closed_url, len(closed_rows))
                                if closed_url else
                                {"tag": "markdown",
                                 "content": f"✅ 已闭环任务（{len(closed_rows)}）"})
        else:
            task_section.append({"tag": "markdown",
                                 "content": "<font color='grey'>✅ 暂无已闭环任务</font>"})
        if doing_rows:
            task_section.append(link_line("🔄 进行中任务", doing_url, len(doing_rows))
                                if doing_url else
                                {"tag": "markdown",
                                 "content": f"🔄 进行中任务（{len(doing_rows)}）"})
        else:
            task_section.append({"tag": "markdown",
                                 "content": "<font color='grey'>🔄 暂无进行中任务</font>"})

    # 📈 趋势：8 字描述 + 折线图链接（应填/填报，非表格）
    trend_head = f"📈 **{desc}**"
    if trend_url:
        trend_head += f"　<font color='grey'>[折线图（应填/填报）→]({trend_url})</font>"
    else:
        trend_head += "　<font color='grey'>（折线图生成中）</font>"

    elements = [
        {"tag": "markdown",
         "content": f"<font color='#8F959E'>🗂 {ledger_src}　🕐 数据截至 {m['as_of']}　👥 组内 {total} 人</font>"},
        {"tag": "hr"},
        # ① 人员概况
        sec("👥 ① 人员概况"),
        tile_row3(
            tile(f"{filled}/{total}", "填报", "green"),
            tile(str(total), "总人数", "blue"),
            tile(str(unfilled), "未按时", "green" if unfilled == 0 else "red"),
        ),
        att_md,
        # ② 目标数量
        sec("🎯 ② 目标数量", ledger_src),
        tile_row3(
            tile(str(goals[0]), "大目标", "blue"),
            tile(str(goals[1]), "小目标", "blue"),
            tile(str(goals[2]), "周期TODO", "blue"),
        ),
        # ③ 完成情况 + 任务链接
        sec("🏁 ③ 完成情况", ledger_src),
        tile_row3(
            tile(str(closure[0]), "已闭环", "green"),
            tile(str(closure[1]), "进行中", "orange"),
            tile(f"{cur_pct:.0f}%", "填报率", "green"),
        ),
        *task_section,
        # ④ 逾期
        sec("⏰ ④ 逾期"),
        lamp("✅" if not overdue_cnt else "⚠️",
             "本周期无逾期" if not overdue_cnt else f"逾期 {overdue_cnt} 条",
             ("明细", od_url) if od_url else None),
        # ⑤ 请假
        sec("🏖 ⑤ 请假", "飞书审批·近周期"),
        lamp("🏖" if g_leaves else "✅",
             f"有请假 {len(g_leaves)} 人" if g_leaves else "无请假",
             ("名单", lv_url) if lv_url else None),
        # ⑥ 评价概况
        sec("⭐ ⑥ 评价概况", ledger_src),
        tile_row3(
            tile(f"{eval_.get('avg', '-')}★", "平均", "orange"),
            tile(str(eval_.get("n", 0)), "已评", "blue"),
            tile(str(len(eval_.get("comments", []))), "评语", "blue"),
        ),
        *([{"tag": "markdown", "content": f"<font color='grey'>{bucket}</font>"}] if bucket else []),
        # 📈 趋势：8 字描述 + 折线图链接
        sec(f"📈 填报趋势（近 {B.TREND_WINDOW} 期 · 非请假口径）"),
        {"tag": "markdown", "content": trend_head},
        # 底部按钮（📌 TODO 行已删除；底部灰字已删除）
        {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
         "columns": [
             {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                 {"tag": "button",
                  "text": {"tag": "plain_text", "content": "📊 打开台账"},
                  "type": "primary", "url": ledger_url}]},
             {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                 {"tag": "button",
                  "text": {"tag": "plain_text", "content": "📋 打开 TODO 总表"},
                  "type": "primary", "url": TODO_LIST_URL}]},
         ]},
    ]

    return {
        "schema": "2.0",
        "config": {"width_mode": "regular", "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📋 周期报表 · {latest} 第{cycle_no}周期 · {mentor}团队"},
            "template": ("red" if (overdue_cnt or unfilled) else
                         "green" if (total and filled == total) else "blue"),
        },
        "body": {"elements": elements},
    }


# ---------------------------------------------------------------------------
# boss 总览卡
# ---------------------------------------------------------------------------
def compute_boss_metrics(people: list[dict], latest: str, date_cols: list[str],
                         exempt: set[str], join_map: dict[str, str]):
    by_mentor: dict[str, list[dict]] = defaultdict(list)
    for p in people:
        by_mentor[p["mentor"] or "(未分组)"].append(p)

    cd = B._cycle_date(latest)
    leave_window, att_window = B.runtime_windows(latest)
    as_of = B.data_as_of()
    cal = B.load_calibration()

    total = len(people)
    filled = sum(1 for p in people if B.member_status(p, latest, exempt, join_map, cd) == "filled")
    unfilled = sum(1 for p in people if B.member_status(p, latest, exempt, join_map, cd) == "unfilled")
    unfilled_names = [p["name"] for p in people
                      if B.member_status(p, latest, exempt, join_map, cd) == "unfilled"]
    not_joined = sum(1 for p in people
                     if B.member_status(p, latest, exempt, join_map, cd) == "not_joined")
    leave_names = {e["name"] for e in B.group_leaves([p["name"] for p in people], leave_window)}
    leave = len(leave_names)

    # 团队维度
    team_rows: list[list[str]] = []
    ledger_rows_by_mentor: dict[str, list[dict]] = {}
    all_closed: list[list[str]] = []
    all_doing: list[list[str]] = []
    for m, members in by_mentor.items():
        member_names = [p["name"] for p in members]
        f = sum(1 for p in members if B.member_status(p, latest, exempt, join_map, cd) == "filled")
        lv = len({e["name"] for e in B.group_leaves(member_names, leave_window)})
        uf = sum(1 for p in members if B.member_status(p, latest, exempt, join_map, cd) == "unfilled")
        ledger = B.load_ledger(m)
        rows = B.ledger_latest_rows(ledger)
        if rows:
            known = B.overdue_from_ledger(rows)
        else:
            known = B.calibration_overdue(cal, m)
        od = len(B._visible_overdue(known, exempt)) + len(
            B.auto_overdue_extra(members, latest, known, exempt, join_map))
        pct = round(f / len(members) * 100) if members else 0
        team_rows.append([m, str(len(members)), str(f), str(uf), str(lv), str(od), f"{pct}%"])
        ledger_rows_by_mentor[m] = rows
        # 全公司任务表（只收集已启用台账的组；未启用台账的组不编数据）
        if rows:
            all_closed += task_rows(rows, "已交付", with_group=True, group=m)
            all_doing += task_rows(rows, "进行中", with_group=True, group=m)
    team_rows.sort(key=lambda r: -int(r[1]))

    # 台账总览（团队｜大｜小｜TODO｜已闭环｜进行中｜逾期｜评分｜来源｜台账）——结构化行，进多维表格
    ledger_srcs = B.load_ledger_sources()
    ledger_rows: list[dict] = []
    name_to_mentor: dict[str, str] = {}
    for m, members in by_mentor.items():
        for p in members:
            name_to_mentor[p["name"]] = m
        rows = ledger_rows_by_mentor.get(m) or []
        if rows:
            g = B.goal_counts_from_ledger(rows) or (0, 0, 0)
            c = B.closure_from_ledger(rows) or (0, 0, 0, 0, 0)
            ev = B.evaluation_from_ledger(rows)
            src = f"台账·截至{(B.load_ledger(m).get('latest_cycle') or '')[-5:] or '本周期'}"
        else:
            g = B.calibration_goals(cal, m) or (0, 0, 0)
            c3 = B.calibration_closure(cal, m) or (0, 0, 0)
            c = (c3[0], c3[1], 0, 0, c3[2])
            ev = None
            src = f"人工核对 {cal.get('calibrated_at', '')[:10]}"
        row: dict = {
            "团队": m, "大": g[0], "小": g[1], "TODO": g[2],
            "已闭环": c[0], "进行中": c[1], "逾期": c[4],
            "来源": src,
        }
        if ev:
            row["评分"] = float(ev["avg"])
            row["评数"] = int(ev["n"])
        lsrc = ledger_srcs.get(m, {})
        if lsrc.get("url"):
            row["台账"] = {"text": "打开台账", "link": lsrc["url"]}
        ledger_rows.append(row)

    # 全公司逾期 / 请假 / 考勤
    all_overdue = []
    for m, members in by_mentor.items():
        ledger = B.load_ledger(m)
        rows = B.ledger_latest_rows(ledger)
        known = B.overdue_from_ledger(rows) if rows else B.calibration_overdue(cal, m)
        for row in B._visible_overdue(known, exempt):
            all_overdue.append((m, *row))
        all_overdue += [(m, *row) for row in
                        B.auto_overdue_extra(members, latest, known, exempt, join_map)]
    anomalies = B.attendance_anomalies({p["name"] for p in people})
    g_leaves = B.group_leaves([p["name"] for p in people], leave_window)

    # 趋势（全公司，非请假口径）
    approvals = B.approved_leaves()

    def on_leave(name: str, d: str) -> bool:
        return any((e.get("start") or "")[:10] <= d <= (e.get("end") or "")[:10]
                   for e in approvals if e.get("name") == name)

    ts = []
    for c in date_cols[-B.TREND_WINDOW:]:
        d = B._cycle_date(c)
        elig = [p for p in people if B.joined_by(join_map, p["name"], d)]
        lv = {p["name"] for p in elig if on_leave(p["name"], d)}
        fl = {p["name"] for p in elig if B._real_fill(p["cols"].get(c) or "")}
        ts.append((c, len(fl - lv), len(elig) - len(lv), len(lv)))
    desc = trend_desc(ts)

    ratio = filled / total if total else 0.0
    return {
        "by_mentor": by_mentor, "total": total, "filled": filled,
        "unfilled": unfilled, "unfilled_names": unfilled_names,
        "not_joined": not_joined, "leave": leave,
        "team_rows": team_rows, "ledger_rows": ledger_rows,
        "name_to_mentor": name_to_mentor,
        "all_closed": all_closed, "all_doing": all_doing,
        "all_overdue": all_overdue, "anomalies": anomalies,
        "g_leaves": g_leaves, "ts": ts, "desc": desc,
        "ratio": ratio, "att_window": att_window, "as_of": as_of,
    }


def build_boss_v6(people: list[dict], latest: str, date_cols: list[str],
                  exempt: set[str], join_map: dict[str, str]) -> dict:
    b = compute_boss_metrics(people, latest, date_cols, exempt, join_map)
    cycle_no = len(date_cols)
    links = boss_links()
    team_url = links["team"]
    bclosed_url = links["closed"]
    bdoing_url = links["doing"]
    ledger_url = links["ledger"]
    overdue_url = links["overdue"]
    leave_url = links["leave"]
    att_url = links["att"]
    boss_trend = trend_url_of("__boss__")

    # 顶部信息行（灰字：口径 / 数据截至 / 结构）
    info = (f"🗂 台账+人工核对　🕐 数据截至 {b['as_of']}　"
            f"👥 在册 {b['total']} 人 · 🏢 {len(b['by_mentor'])} 团队")

    # 灰底数字格（与 mentor 卡同构，26px 语义色 + 灰标签，boss 一眼拿到关键数字）
    unfilled_col = "red" if b["unfilled"] else "green"
    overdue_col = "red" if b["all_overdue"] else "green"
    att_col = "red" if b["anomalies"] else "green"
    leave_col = "orange" if b["g_leaves"] else "green"
    doing_col = "orange" if b["all_doing"] else "green"

    # 趋势行（链接版）
    trend_head = f"📈 **{b['desc']}**"
    if boss_trend:
        trend_head += f"　<font color='grey'>[折线图（应填/填报）→]({boss_trend})</font>"
    else:
        trend_head += "　<font color='grey'>（折线图生成中）</font>"

    elements = [
        {"tag": "markdown", "content": f"<font color='#8F959E'>{info}</font>"},
        {"tag": "hr"},
        # ① 人员与填报（灰底数字格）
        sec("👥 ① 人员与填报"),
        tile_row3(
            tile(f"{b['filled']}/{b['total']}", "填报", "green"),
            tile(str(b["total"]), "在册", "blue"),
            tile(str(b["unfilled"]), "未按时", unfilled_col),
        ),
        *([{"tag": "markdown",
            "content": f"⚠️ <font color='#F53F3F'>未按时：{esc('、'.join(b['unfilled_names']))}</font>"}]
          if b["unfilled"] else []),
        *([{"tag": "markdown",
            "content": f"📅 未入职（本周期不参与统计）：{b['not_joined']} 人"}]
          if b["not_joined"] else []),
        # ② 任务与台账（灰底数字格）
        sec("📊 ② 任务与台账"),
        tile_row3(
            tile(str(len(b["all_closed"])), "已闭环", "green"),
            tile(str(len(b["all_doing"])), "进行中", doing_col),
            tile(str(len(b["all_overdue"])), "逾期", overdue_col),
        ),
        # ③ 团队与考勤（灰底数字格）
        sec("🏢 ③ 团队与考勤"),
        tile_row3(
            tile(str(len(b["team_rows"])), "团队", "blue"),
            tile(str(len(b["g_leaves"])), "请假", leave_col),
            tile(str(len(b["anomalies"])), "考勤异常", att_col),
        ),
        {"tag": "hr"},
        # 团队维度（链接 → 多维表格）
        sec(f"🏢 团队维度（{len(b['team_rows'])} 组）"),
        *([{"tag": "markdown",
            "content": f"{len(b['team_rows'])} 组 · 人数/填报/未填/请假/逾期/填报率　"
                       f"<font color='grey'>[打开明细 →]({team_url})</font>"}]
          if team_url else
          [{"tag": "markdown", "content": "<font color='grey'>（明细表生成中）</font>"}]),
        # 台账总览（链接 → 多维表格）
        sec("🗂 台账总览（各组 ②③⑥）"),
        *([{"tag": "markdown",
            "content": f"{len(b['ledger_rows'])} 组 · 大/小/TODO/已闭环/进行中/逾期/评分　"
                       f"<font color='grey'>[打开明细 →]({ledger_url})</font>"}]
          if ledger_url else
          [{"tag": "markdown", "content": "<font color='grey'>（明细表生成中）</font>"}]),
        # 已闭环 / 进行中任务（链接 → 多维表格）
        sec(f"✅ 已闭环任务（{len(b['all_closed'])}）"),
        *([{"tag": "markdown",
            "content": f"全公司已交付任务 {len(b['all_closed'])} 条　"
                       f"<font color='grey'>[明细 →]({bclosed_url})</font>"}]
          if bclosed_url else
          [{"tag": "markdown", "content": "<font color='grey'>暂无（台账未启用或无已闭环任务）</font>"}]),
        sec(f"🔄 进行中任务（{len(b['all_doing'])}）"),
        *([{"tag": "markdown",
            "content": f"全公司进行中任务 {len(b['all_doing'])} 条　"
                       f"<font color='grey'>[明细 →]({bdoing_url})</font>"}]
          if bdoing_url else
          [{"tag": "markdown", "content": "<font color='grey'>暂无（台账未启用或无进行中任务）</font>"}]),
        {"tag": "hr"},
        # 逾期 / 请假 / 考勤（v7：全部走链接 → 多维表格）
        sec(f"⚠️ 逾期明细（全公司 {len(b['all_overdue'])} 条）"),
        *([{"tag": "markdown",
            "content": f"全公司逾期 {len(b['all_overdue'])} 条 · 负责人/任务/延期说明　"
                       f"<font color='grey'>[明细 →]({overdue_url})</font>"}]
          if overdue_url else
          [{"tag": "markdown",
            "content": "✅ 全公司本周期无逾期" if not b["all_overdue"]
                       else "<font color='grey'>（明细表生成中）</font>"}]),
        sec(f"🏖 请假标注（飞书审批 · 近周期 {len(b['g_leaves'])} 人）"),
        *([{"tag": "markdown",
            "content": f"近周期已批准请假 {len(b['g_leaves'])} 人 · 姓名/团队/类型/起止/天数　"
                       f"<font color='grey'>[明细 →]({leave_url})</font>"}]
          if leave_url else
          [{"tag": "markdown",
            "content": "本周期窗口内无已批准请假" if not b["g_leaves"]
                       else "<font color='grey'>（明细表生成中）</font>"}]),
        sec(f"⏰ 考勤异常（{b['att_window']} · 真实打卡）"),
        *([{"tag": "markdown",
            "content": f"本窗口 {len(b['anomalies'])} 人异常 · 姓名/缺卡/迟到/早退　"
                       f"<font color='grey'>[明细 →]({att_url})</font>"}]
          if att_url else
          [{"tag": "markdown",
            "content": "✅ 无迟到/早退/缺卡" if not b["anomalies"]
                       else "<font color='grey'>（明细表生成中）</font>"}]),
        # 趋势（链接版）
        sec(f"📈 填报趋势（近 {B.TREND_WINDOW} 期全公司 · 非请假口径）"),
        {"tag": "markdown", "content": trend_head},
        {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
         "columns": [
             {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                 {"tag": "button",
                  "text": {"tag": "plain_text", "content": "📋 打开 TODO 总表"},
                  "type": "primary", "url": TODO_LIST_URL}]},
         ]},
    ]

    return {
        "schema": "2.0",
        "config": {"width_mode": "regular", "enable_forward": True},
        "header": {
            "title": {"tag": "plain_text",
                      "content": f"📊 全公司 TODO 总览 · {latest} 第{cycle_no}周期"},
            "template": ("red" if (b["unfilled"] or b["all_overdue"]) else
                         "green" if b["ratio"] >= 1.0 else "blue"),
        },
        "body": {"elements": elements},
    }


# ---------------------------------------------------------------------------
# 趋势序列输出（供海豚用 feishu_chart 生成折线图 docx）
# ---------------------------------------------------------------------------
def write_trend_data(by_mentor: dict[str, list[dict]], people: list[dict],
                     latest: str, date_cols: list[str], exempt: set[str],
                     join_map: dict[str, str]) -> None:
    approvals = B.approved_leaves()

    def on_leave(name: str, d: str) -> bool:
        return any((e.get("start") or "")[:10] <= d <= (e.get("end") or "")[:10]
                   for e in approvals if e.get("name") == name)

    def series_for(group: list[dict]) -> tuple[list[str], dict]:
        labels, ying, tian = [], [], []
        for c in date_cols[-B.TREND_WINDOW:]:
            d = B._cycle_date(c)
            elig = [p for p in group if B.joined_by(join_map, p["name"], d)]
            lv = {p["name"] for p in elig if on_leave(p["name"], d)}
            fl = {p["name"] for p in elig if B._real_fill(p["cols"].get(c) or "")}
            labels.append(c)
            ying.append(len(elig) - len(lv))
            tian.append(len(fl - lv))
        return labels, {"应填": ying, "填报": tian}

    out: dict[str, dict] = {}
    for mentor, members in by_mentor.items():
        if mentor == "(未分组)":
            continue
        labels, series = series_for(members)
        out[mentor] = {"labels": labels, "series": series,
                       "title": f"{mentor}组 · 填报趋势（应填/填报 · 非请假口径）"}
    labels, series = series_for(people)
    out["__boss__"] = {"labels": labels, "series": series,
                       "title": "全公司 · 填报趋势（应填/填报 · 非请假口径）"}
    TREND_DATA.parent.mkdir(parents=True, exist_ok=True)
    TREND_DATA.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[trend] 趋势序列 → {TREND_DATA}（{len(out)} 份）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    date_cols, people = B.load_people(None)
    if not date_cols:
        print("[err] 没有周期列（TODO LIST 数据源为空）", file=sys.stderr)
        return 1
    latest = date_cols[-1]
    leave_window, att_window = B.runtime_windows(latest)
    exempt = B.leave_exempt_names(leave_window)
    join_map = B.join_dates()
    print(f"[data] 最新周期 {latest}（第 {len(date_cols)} 周期）· 请假窗口 {leave_window} · "
          f"考勤窗口 {att_window} · 豁免 {len(exempt)} 人")

    by_mentor: dict[str, list[dict]] = defaultdict(list)
    for p in people:
        by_mentor[p["mentor"] or "(未分组)"].append(p)
    mentor_names = [m for m in by_mentor if m != "(未分组)"]
    oids = B.mentor_oids()

    # ① 任务明细多维表格（幂等建表/刷新 + 注册链接）
    for mentor in mentor_names:
        members = by_mentor.get(mentor, [])
        m = compute_mentor_metrics(mentor, members, latest, date_cols, exempt, join_map)
        closed_rows = task_rows(m["rows"], "已交付")
        doing_rows = task_rows(m["rows"], "进行中")
        ensure_mentor_task_tables(mentor, closed_rows, doing_rows)
        print(f"[任务表] {mentor}: 已闭环 {len(closed_rows)} · 进行中 {len(doing_rows)}")
    boss_m = compute_boss_metrics(people, latest, date_cols, exempt, join_map)
    ensure_boss_tables(boss_m["team_rows"], boss_m["all_closed"], boss_m["all_doing"],
                       boss_m["ledger_rows"], boss_m["all_overdue"],
                       boss_m["g_leaves"], boss_m["anomalies"], boss_m["att_window"],
                       boss_m["name_to_mentor"])
    print(f"[任务表] boss: 团队维度 {len(boss_m['team_rows'])} 组 · 已闭环 {len(boss_m['all_closed'])} · "
          f"进行中 {len(boss_m['all_doing'])} · 台账 {len(boss_m['ledger_rows'])} 组 · "
          f"逾期 {len(boss_m['all_overdue'])} · 请假 {len(boss_m['g_leaves'])} · "
          f"考勤 {len(boss_m['anomalies'])} 人")

    # ② 趋势序列输出（海豚据此生成折线图 docx 并注册 trend）
    write_trend_data(by_mentor, people, latest, date_cols, exempt, join_map)

    # ③ 生成卡片（无表格，链接版）
    out: dict[str, dict] = {}
    for mentor in mentor_names:
        members = by_mentor.get(mentor, [])
        oid = oids.get(mentor, "")
        if not oid:
            print(f"[warn] mentor {mentor} 无 open_id，跳过", file=sys.stderr)
            continue
        card = build_mentor_v6(mentor, members, latest, date_cols, exempt, join_map)
        out[mentor] = {"oid": oid, "card": card}
        print(f"[mentor] {mentor}（{len(members)}人）卡片完成 · "
              f"template={card['header']['template']}")

    boss = build_boss_v6(people, latest, date_cols, exempt, join_map)
    out["__boss__"] = {"card": boss}
    print(f"[boss] 总览卡完成（{len(boss['body']['elements'])} 个元素）")

    # ④ 刷新已注册 mentor 的自建明细表（请假/逾期/考勤）
    for mentor in mentor_names:
        reg = (_load_detail_bases().get(mentor) or {})
        if not (reg and reg.get("app_token")):
            continue
        m = compute_mentor_metrics(mentor, by_mentor.get(mentor, []), latest,
                                   date_cols, exempt, join_map)
        refresh_registered_tables(mentor, m["g_leaves"], m["overdue"], m["anomalies"],
                                  m["att_window"])

    # 输出
    dest = WS / "mentor-cards" / "mentor_cards_v9.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] 全部卡片 → {dest}")

    if "孙逊" in out:
        sun = WS / "六区数字卡-真实卡片v9.json"
        sun.write_text(json.dumps(out["孙逊"]["card"], ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"[ok] 孙逊组卡 → {sun}")
    bfile = WS / "六区数字卡-真实卡片v9-boss.json"
    bfile.write_text(json.dumps(boss, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[ok] boss 卡 → {bfile}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
