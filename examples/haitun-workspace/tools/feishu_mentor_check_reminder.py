# ruff: noqa: RUF001, RUF002, RUF003  # 中文全角标点是刻意排版
"""Mentor 检查确认卡（3 点后提醒 mentor 检查下级 todo list 写得怎么样）。

给一位 mentor 私聊发一张卡：列出他组内成员本周期的填报/规范概况，底部一个
「✅ 已检查完成」按钮。mentor 检查完点一下，卡片原地更新为「已确认检查完成」
（记录确认人/时间），按钮消失，避免重复点。

数据源：``todo_list_parsed.json``（组织结构 people[{name,mentor,cols}]）+
``feishu_todo_spec_check`` 的规范判定（同一口径）。纯确定性。

一个工具两用：
- 发提醒：``feishu_mentor_check_reminder(mentor_open_id=..., mentor_name=...)``
- 处理点击：``feishu_mentor_check_reminder(card_action_json=<payload>)``
  —— action=``mentor_check_done`` 时记确认并 edit_card 原地更新。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path
from typing import Any

import _feishu_impl as _core
from _runtime_paths import agent_dir

_NUM_FONT = "26px"
_SIGN = "海豚三号"
_STATE_DIR = "mentor-check-state"  # AppData 下记录各 mentor 本周期确认状态


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    """卡片生成时刻（本地时间，MM-DD HH:MM）——保证卡面时间永远是最新的。"""
    return datetime.datetime.now().strftime("%m-%d %H:%M")


def _load_roster_oids(agent_root: str) -> dict[str, str]:
    """roster.json → {姓名: open_id}（本 app 通讯录；跨 app open_id 生产环境需重新解析）。"""
    p = Path(agent_dir(agent_root)) / "mentor-cards" / "roster.json"
    try:
        r = json.loads(p.read_text(encoding="utf-8"))
        return {m["name"]: (m.get("open_id") or "") for m in r.get("members", [])}
    except (ValueError, OSError):
        return {}


def _seg_state(seg: str, S) -> str:
    """某一级（大/小目标 或 todo）的状态：none(无) / ok(有且有截止) / nodue(有但缺截止)。"""
    if not seg.strip():
        return "none"
    return "ok" if S._has_due(seg) else "nodue"


def _tile(n: str, lab: str, color: str = "default") -> dict:
    tc = {"green": "#34C724", "blue": "#3370FF", "orange": "#FA8C16", "red": "#F53F3F"}
    c = tc.get(color, "default")
    num = (f"<font size='{_NUM_FONT}' color='{c}'>**{n}**</font>" if c != "default"
           else f"<font size='{_NUM_FONT}'>**{n}**</font>")
    return {"tag": "column", "width": "weighted", "weight": 1, "vertical_align": "top",
            "elements": [
                {"tag": "markdown", "content": num, "text_align": "center"},
                {"tag": "markdown", "content": f"<font color='grey'>{lab}</font>",
                 "text_align": "center"}]}


def _tile_row3(*tiles: dict) -> dict:
    return {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "4px",
            "background_style": "grey", "columns": list(tiles)}


def _group_overview(agent_root: str, mentor_name: str) -> dict:
    """算某 mentor 组内成员本周期的填报/规范概况。"""
    mc = Path(agent_dir(agent_root)) / "mentor-cards"
    if str(mc) not in sys.path:
        sys.path.insert(0, str(mc))
    import build_cards as B  # noqa: PLC0415
    sys.path.insert(0, str(Path(agent_dir(agent_root)) / "tools"))
    import feishu_todo_spec_check as S  # noqa: PLC0415

    date_cols, people = B.load_people(None)
    latest = date_cols[-1] if date_cols else ""
    cycle_no = len(date_cols)
    leave_window, _ = B.runtime_windows(latest) if latest else ((None, None), None)
    exempt = B.leave_exempt_names(leave_window) if latest else set()
    join_map = B.join_dates()
    cd = B._cycle_date(latest) if latest else None

    name2oid = _load_roster_oids(agent_root)
    members = [p for p in people if (p.get("mentor") or "") == mentor_name]
    rows: list[dict] = []
    filled = unfilled = leave = 0
    spec_red = spec_orange = 0
    for p in members:
        st = B.member_status(p, latest, exempt, join_map, cd)
        raw = (p["cols"].get(latest) or "").strip()
        oid = name2oid.get(p["name"], "")
        if st == "filled":
            filled += 1
            chk = S._check_person(p["name"], raw)
            sec = S._split_sections(raw)
            levels = {k: _seg_state(sec.get(k, ""), S) for k in ("big", "small", "todo")}
            rows.append({"name": p["name"], "open_id": oid, "status": "filled",
                         "level": chk["level"], "issues": chk["issues"], "levels": levels})
            if chk["level"] == "red":
                spec_red += 1
            elif chk["level"] == "orange":
                spec_orange += 1
        elif st == "unfilled":
            unfilled += 1
            rows.append({"name": p["name"], "open_id": oid, "status": "unfilled",
                         "level": "red", "issues": ["未填报"]})
        elif st == "leave":
            leave += 1
            rows.append({"name": p["name"], "open_id": oid, "status": "leave",
                         "level": "grey", "issues": []})
    return {"latest": latest, "cycle_no": cycle_no, "members": rows,
            "total": len(members), "filled": filled, "unfilled": unfilled,
            "leave": leave, "spec_red": spec_red, "spec_orange": spec_orange,
            "as_of": B.data_as_of()}


# 卡片构造与主函数在下方追加


_LEVEL_ICON = {"red": "🔴", "orange": "🟠", "green": "🟢", "grey": "⚪"}
_STATUS_LABEL = {"filled": "", "unfilled": "未填报", "leave": "请假"}


def _member_status_cell(m: dict) -> str:
    icon = _LEVEL_ICON.get(m["level"], "⚪")
    if m["status"] == "leave":
        tail = "<font color='grey'>请假</font>"
    elif m["status"] == "unfilled":
        tail = "<font color='#F53F3F'>未填报</font>"
    elif m["issues"]:
        tail = _esc(" / ".join(m["issues"][:2]))
    else:
        tail = "<font color='#34C724'>规范</font>"
    return f"{icon} **{_esc(m['name'])}** — {tail}"


_FILL_TXT = {"filled": "<font color='#34C724'>已填</font>",
             "unfilled": "<font color='#F53F3F'>未填</font>",
             "leave": "<font color='grey'>请假</font>"}
_SPEC_CELL = {"green": "<font color='#34C724'>规范</font>",
              "orange": "<font color='#FA8C16'>待完善</font>",
              "red": "<font color='#F53F3F'>缺必填</font>",
              "grey": "<font color='grey'>—</font>"}


# 三级状态 → 单元格文案（ok=绿√ / nodue=橙缺截止 / none=灰无）
_SEG_CELL = {"ok": "<font color='#34C724'>✓</font>",
             "nodue": "<font color='#FA8C16'>缺截止</font>",
             "none": "<font color='#B0B6BF'>—</font>"}
_WEIGHTS = [3, 2, 2, 2, 3]  # 姓名 大目标 小目标 TODO 操作


def _row5(cells: list[dict], bg: str = "") -> dict:
    """五列一行：姓名 | 大目标 | 小目标 | TODO | 操作。bg='grey' 作表头底色。"""
    cs = {"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "4px",
          "columns": [{"tag": "column", "width": "weighted", "weight": w,
                       "vertical_align": "center", "elements": [c]}
                      for w, c in zip(_WEIGHTS, cells)]}
    if bg:
        cs["background_style"] = bg
    return cs


def _hdr(text: str, align: str = "left") -> dict:
    return {"tag": "markdown", "content": f"<font color='#646A73'>**{text}**</font>", "text_align": align}


def _member_table(ov: dict, mentor_name: str, with_buttons: bool = True) -> list[dict]:
    """组内成员表格：表头（姓名｜大目标｜小目标｜TODO｜操作）+ 每行五列对齐。

    大/小目标/TODO 列显示各级状态（✓=有且合规 / 缺截止 / —=无）；
    操作列是「提醒 TA」按钮，已响应显示「已收到/已修正」。
    """
    responded = ov.get("responded", {})  # {name: "ack"|"fixed"}
    out: list[dict] = [
        _row5([_hdr("姓名"), _hdr("大目标", "center"), _hdr("小目标", "center"),
               _hdr("TODO", "center"), _hdr("操作", "center")], bg="grey"),
    ]
    for m in ov["members"]:
        name_cell = {"tag": "markdown", "content": f"**{_esc(m['name'])}**"}
        lv = m.get("levels", {})
        if m["status"] == "filled":
            big = {"tag": "markdown", "content": _SEG_CELL.get(lv.get("big", "none"), "—"), "text_align": "center"}
            sml = {"tag": "markdown", "content": _SEG_CELL.get(lv.get("small", "none"), "—"), "text_align": "center"}
            tdo = {"tag": "markdown", "content": _SEG_CELL.get(lv.get("todo", "none"), "—"), "text_align": "center"}
        elif m["status"] == "unfilled":
            cell = {"tag": "markdown", "content": "<font color='#F53F3F'>未填</font>", "text_align": "center"}
            big, sml, tdo = cell, dict(cell), dict(cell)
        else:  # leave
            cell = {"tag": "markdown", "content": "<font color='grey'>请假</font>", "text_align": "center"}
            big, sml, tdo = cell, dict(cell), dict(cell)
        resp = responded.get(m["name"])
        if resp:
            op_cell = {"tag": "markdown",
                       "content": ("✅ <font color='#34C724'>已修正</font>" if resp == "fixed"
                                   else "📨 <font color='#3370FF'>已收到</font>"),
                       "text_align": "center"}
        elif with_buttons and m["status"] != "leave" and m.get("open_id"):
            op_cell = {"tag": "button", "size": "tiny",
                       "text": {"tag": "plain_text", "content": "提醒 TA"},
                       "type": "default",
                       "behaviors": [{"type": "callback",
                                      "value": {"action": "notify_member",
                                                "member_name": m["name"],
                                                "member_open_id": m["open_id"],
                                                "mentor_name": mentor_name,
                                                "cycle": ov["latest"]}}]}
        else:
            op_cell = {"tag": "markdown",
                       "content": ("<font color='#B0B6BF'>—</font>" if m["status"] == "leave"
                                   else "<font color='#B0B6BF'>无ID</font>"), "text_align": "center"}
        out.append(_row5([name_cell, big, sml, tdo, op_cell]))
    # 图例
    out.append({"tag": "markdown",
                "content": "<font color='#B0B6BF'>✓=已填且合规 · 缺截止=有内容但没写截止 · —=该级没写</font>"})
    return out


def _build_reminder_card(ov: dict, mentor_name: str, todo_url: str = "",
                         done: bool = False, done_by: str = "", done_at: str = "",
                         responded: dict | None = None) -> dict:
    """mentor 检查确认卡。done=True 渲染已确认态（无按钮）。

    responded: {组员名: "ack"|"fixed"}，非空则表格对应行操作列显示「已收到/已修正」，
    这就是组员反馈回流到组长卡的呈现。
    """
    if responded:
        ov = {**ov, "responded": {**ov.get("responded", {}), **responded}}
    template = "green" if done else ("red" if (ov["unfilled"] or ov["spec_red"]) else "blue")
    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"<font color='#8F959E'>🕐 {_now()} 生成 · {ov['latest']} 第{ov['cycle_no']}周期 · "
                    f"数据截至 {ov['as_of']}</font>"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "<font color='#1F2329'>**👥 组内填报概况**</font>"},
        _tile_row3(
            _tile(f"{ov['filled']}/{ov['total']}", "已填报", "green"),
            _tile(str(ov["unfilled"]), "未填报", "red" if ov["unfilled"] else "green"),
            _tile(str(ov["spec_red"] + ov["spec_orange"]), "待完善", "orange")),
        {"tag": "markdown",
         "content": "<font color='#1F2329'>**📋 成员明细**</font>"
                    "<font color='#B0B6BF'>（点「提醒 TA」直接发卡给该组员）</font>"},
    ]
    elements.extend(_member_table(ov, mentor_name, with_buttons=not done))
    if todo_url:
        elements.append({"tag": "markdown",
                         "content": f"<font color='grey'>[打开 TODO LIST 核查 →]({todo_url})</font>"})
    elements.append({"tag": "hr"})

    if done:
        elements.append({"tag": "markdown",
                         "content": f"✅ <font color='#34C724'>**已确认检查完成**</font>"
                                    + (f"　<font color='grey'>{_esc(done_by)} · {done_at}</font>"
                                       if done_by else "")})
    else:
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": "✅ 已检查完成"},
            "type": "primary",
            "behaviors": [{"type": "callback",
                           "value": {"action": "mentor_check_done",
                                     "mentor_name": mentor_name, "cycle": ov["latest"]}}]})
    elements.append({"tag": "markdown", "content": f"<font color='#75726F'>{_SIGN}</font>"})
    return {"schema": "2.0", "config": {"width_mode": "regular"},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"🧑‍🏫 请检查组内 TODO · {mentor_name}团队 · {ov['latest']}"},
                       "template": template},
            "body": {"elements": elements}}


async def _state_path(mentor_name: str, cycle: str) -> Any:
    import anyio  # noqa: PLC0415

    from psi_agent._appdata import resolve_appdata_root  # noqa: PLC0415
    root = await resolve_appdata_root("")
    d = anyio.Path(root) / _STATE_DIR
    await d.mkdir(parents=True, exist_ok=True)
    slug = f"{mentor_name}_{cycle}".replace("/", "_").replace("\\", "_")
    return d / f"{slug}.json"


def _parse_action(card_action_json: str) -> dict[str, Any]:
    try:
        payload = json.loads(card_action_json) if card_action_json.strip() else {}
    except ValueError:
        return {}
    action = payload.get("action") if isinstance(payload, dict) else None
    value = action.get("value") if isinstance(action, dict) else None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {}
    out = dict(value) if isinstance(value, dict) else {}
    out["_message_id"] = payload.get("message_id") or payload.get("open_message_id") or ""
    out["_operator"] = ((payload.get("operator") or {}).get("open_id")
                        if isinstance(payload.get("operator"), dict) else "") or ""
    return out


async def feishu_mentor_check_reminder(
    mentor_open_id: str = "",
    mentor_name: str = "",
    todo_list_url: str = "",
    card_action_json: str = "",
    user_key: str = "",
    agent_root: str = "",
) -> str:
    """3 点后提醒 mentor 检查下级 todo list，带「已检查完成」确认按钮。

    一个工具两用：

    **发提醒**（传 mentor_open_id + mentor_name）：读组织结构，算组内成员本周期
    填报/规范概况，渲染 v9 六区数字格卡片（已填/未填/待完善三列 + 成员逐行明细），
    底部「✅ 已检查完成」按钮，私聊发给 mentor。

    **处理点击**（传 card_action_json）：action=``mentor_check_done`` 时，记录该
    mentor 本周期已确认检查（存 AppData），并 edit_card 把卡片原地更新为「已确认
    检查完成 · 确认人 · 时间」态，按钮消失。发送时须带
    ``action_handlers_json={"mentor_check_done":"feishu_mentor_check_reminder"}``。

    Args:
        mentor_open_id: 收卡 mentor 的 open_id（发提醒时必填）。
        mentor_name: mentor 团队名（发提醒时必填；也用于组内成员筛选）。
        todo_list_url: TODO LIST 链接（卡面「打开核查」）。
        card_action_json: 点击回调 payload（处理点击时传）。
        user_key: 调用者/点击者 open_id。
        agent_root: workspace 根（留空自动解析）。

    Returns:
        JSON：ok / action(sent|checked_done) / message_id / counts / error。
    """
    # ── 处理点击 ────────────────────────────────────────────────────────
    if card_action_json.strip():
        act = _parse_action(card_action_json)
        action = act.get("action") or ""

        # 组长点某行「提醒 TA」→ 给该组员发组员个人卡（带反馈按钮）
        if action == "notify_member":
            member_name = act.get("member_name") or ""
            member_oid = act.get("member_open_id") or ""
            if not member_oid:
                return json.dumps({"ok": False, "error": "member has no open_id (roster/跨app)"},
                                  ensure_ascii=False)
            try:
                sys.path.insert(0, str(Path(agent_dir(agent_root)) / "tools"))
                import feishu_member_todo_card as MC  # noqa: PLC0415
                out = await MC.feishu_member_todo_card(
                    receive_id=member_oid, member_name=member_name,
                    todo_list_url=todo_list_url, agent_root=agent_root,
                    from_mentor=act.get("mentor_name") or "", with_feedback=True)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": False, "error": f"notify member failed: {e!r}"},
                                  ensure_ascii=False)
            return json.dumps({"ok": True, "action": "notified_member",
                               "member": member_name, "detail": json.loads(out)},
                              ensure_ascii=False, default=str)

        # 整卡「已检查完成」
        if action != "mentor_check_done" and "mentor_name" not in act:
            return json.dumps({"ok": False, "error": "unrecognized card action"}, ensure_ascii=False)
        m_name = act.get("mentor_name") or mentor_name
        cycle = act.get("cycle") or ""
        msg_id = act.get("_message_id") or ""
        operator = act.get("_operator") or user_key
        now = datetime.datetime.now().strftime("%m-%d %H:%M")
        try:
            import anyio  # noqa: PLC0415
            sp = await _state_path(m_name, cycle)
            await anyio.Path(sp).write_text(json.dumps(
                {"mentor": m_name, "cycle": cycle, "done_by": operator, "done_at": now},
                ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"save state failed: {e!r}"}, ensure_ascii=False)
        # 原地更新卡片为已确认态
        if msg_id:
            try:
                ov = _group_overview(agent_root, m_name)
                card = _build_reminder_card(ov, m_name, todo_list_url.strip(),
                                            done=True, done_by="已确认", done_at=now)
                await _core.edit_card_impl(msg_id, json.dumps(card, ensure_ascii=False), user_key)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": True, "action": "checked_done",
                                   "warn": f"state saved but card update failed: {e!r}"},
                                  ensure_ascii=False)
        return json.dumps({"ok": True, "action": "checked_done", "mentor": m_name,
                           "cycle": cycle, "done_at": now}, ensure_ascii=False)

    # ── 发提醒卡 ────────────────────────────────────────────────────────
    if not mentor_open_id.strip() or not mentor_name.strip():
        return json.dumps({"ok": False, "error": "mentor_open_id and mentor_name are required"},
                          ensure_ascii=False)
    try:
        ov = _group_overview(agent_root, mentor_name.strip())
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"overview failed: {e!r}"}, ensure_ascii=False)
    if ov["total"] == 0:
        return json.dumps({"ok": False, "error": f"mentor {mentor_name} has no group members"},
                          ensure_ascii=False)
    card = _build_reminder_card(ov, mentor_name.strip(), todo_list_url.strip())
    res = await _core.send_card_impl(
        receive_id=mentor_open_id.strip(),
        card_json=json.dumps(card, ensure_ascii=False),
        receive_id_type="open_id",
        user_key=user_key,
        business_context_json=json.dumps(
            {"kind": "mentor_check_reminder", "mentor_name": mentor_name, "cycle": ov["latest"]},
            ensure_ascii=False),
        action_handlers_json=json.dumps(
            {"mentor_check_done": "feishu_mentor_check_reminder",
             "notify_member": "feishu_mentor_check_reminder"}, ensure_ascii=False),
        multi_use=True,  # 每行「提醒 TA」独立点，整卡「已检查完成」也在其中
    )
    if not res.get("ok"):
        return json.dumps({"ok": False, "error": res.get("message") or res.get("error") or res},
                          ensure_ascii=False, default=str)
    return json.dumps({"ok": True, "action": "sent", "message_id": res.get("message_id"),
                       "counts": {"total": ov["total"], "filled": ov["filled"],
                                  "unfilled": ov["unfilled"],
                                  "spec_issues": ov["spec_red"] + ov["spec_orange"]}},
                      ensure_ascii=False, default=str)
