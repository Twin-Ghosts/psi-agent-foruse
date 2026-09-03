# ruff: noqa: RUF001, RUF002, RUF003  # 中文全角标点是刻意排版
"""组员个人 TODO 卡（发给组员本人）。

一张卡把组员本周期的情况说清楚：① 填了没（填报状态）② 写得规不规范
（复用 feishu_todo_spec_check 判定）③ 本周期 todo 清单（从填报文本提取）
④「去修改 TODO LIST」按钮。v9 六区数字格，时间戳=当前生成时刻（永远最新）。

数据源：``todo_list_parsed.json``（本人 cols[本周期] 原始填报文本）+
``build_cards.member_status``（填报口径）+ ``feishu_todo_spec_check``（规范口径）。
纯确定性、无需大模型。区别于 mentor 检查卡（那是给组长看全组的）。
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import _feishu_impl as _core
from _runtime_paths import agent_dir

_NUM_FONT = "26px"
_SIGN = "海豚三号"
_TCOLOR = {"green": "#34C724", "blue": "#3370FF", "orange": "#FA8C16", "red": "#F53F3F"}


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    return datetime.datetime.now().strftime("%m-%d %H:%M")


def _tile(n: str, lab: str, color: str = "default") -> dict:
    c = _TCOLOR.get(color, "default")
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


def _extract_todos(todo_section: str) -> list[str]:
    """从 todo 段抽出条目（按行/序号），最多列 6 条。"""
    items: list[str] = []
    for ln in todo_section.splitlines():
        s = ln.strip(" \t-·•　0123456789.、)）")
        # 跳过纯标题行
        if not s or s in ("todo", "TODO", "待办", "任务") or s.startswith(("todo", "TODO", "待办")):
            continue
        items.append(s if len(s) <= 40 else s[:40] + "…")
    return items[:6]


def _gather(agent_root: str, name: str) -> dict:
    """汇总组员本人的填报状态 + 规范 + todo 清单。"""
    mc = Path(agent_dir(agent_root)) / "mentor-cards"
    if str(mc) not in sys.path:
        sys.path.insert(0, str(mc))
    tools = Path(agent_dir(agent_root)) / "tools"
    if str(tools) not in sys.path:
        sys.path.insert(0, str(tools))
    import build_cards as B  # noqa: PLC0415
    import feishu_todo_spec_check as S  # noqa: PLC0415

    date_cols, people = B.load_people(None)
    latest = date_cols[-1] if date_cols else ""
    cycle_no = len(date_cols)
    leave_window, _ = B.runtime_windows(latest) if latest else ((None, None), None)
    exempt = B.leave_exempt_names(leave_window) if latest else set()
    join_map = B.join_dates()
    cd = B._cycle_date(latest) if latest else None

    person = next((p for p in people if p["name"] == name), None)
    if person is None:
        return {"found": False, "latest": latest, "cycle_no": cycle_no}
    raw = (person["cols"].get(latest) or "").strip()
    status = B.member_status(person, latest, exempt, join_map, cd)
    spec = S._check_person(name, raw)
    sec = S._split_sections(raw)
    todos = _extract_todos(sec.get("todo", ""))
    return {"found": True, "name": name, "mentor": person.get("mentor", ""),
            "latest": latest, "cycle_no": cycle_no, "status": status,
            "spec": spec, "todos": todos, "as_of": B.data_as_of()}


# 卡片构造与主函数在下方追加


_STATUS_TXT = {"filled": ("已填报", "green"), "unfilled": ("未填报", "red"),
               "leave": ("请假豁免", "grey"), "not_joined": ("未入职", "grey")}
_SPEC_TXT = {"green": ("规范", "green"), "orange": ("待完善", "orange"), "red": ("缺必填", "red")}


def _build_member_card(g: dict, todo_url: str = "", from_mentor: str = "",
                        with_feedback: bool = False, responded: str = "") -> dict:
    """组员个人卡：填报状态 + 规范 + todo 清单 + 去修改按钮 + 可选反馈按钮。

    from_mentor: 非空则卡头标注「<mentor> 提醒你检查」。
    with_feedback: True 则底部加「✅ 已收到」「已按要求修正」反馈按钮。
    responded: "ack"/"fixed" 时渲染已反馈态（按钮消失）。
    """
    name = g["name"]
    status = g["status"]
    spec = g["spec"]
    st_txt, st_c = _STATUS_TXT.get(status, ("未知", "grey"))
    sp_txt, sp_c = _SPEC_TXT.get(spec["level"], ("—", "grey"))
    filled = 1 if status == "filled" else 0
    issue_n = len([i for i in spec["issues"]]) if status == "filled" else 0

    # 卡头：未填=红；填了但缺必填=红；待完善=橙；规范=绿
    if status != "filled":
        template = "red" if status == "unfilled" else "grey"
    else:
        template = {"red": "red", "orange": "orange", "green": "green"}[spec["level"]]

    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"<font color='#8F959E'>🕐 {_now()} 生成 · {g['latest']} 第{g['cycle_no']}周期 · "
                    f"数据截至 {g['as_of']}</font>"},
        {"tag": "hr"},
        _tile_row3(
            _tile(f"{filled}/1", "填报", st_c),
            _tile(sp_txt, "规范", sp_c),
            _tile(str(issue_n), "待改项", "red" if issue_n else "green")),
    ]
    if from_mentor:
        elements.insert(1, {"tag": "markdown",
                            "content": f"<font color='#3370FF'>📣 **{_esc(from_mentor)}** 提醒你检查本周期 TODO 填报</font>"})

    # 填报状态说明
    if status == "unfilled":
        elements.append({"tag": "markdown",
                         "content": "⚠️ <font color='#F53F3F'>你本周期还没填写 TODO LIST，请尽快补上。</font>"})
    elif status == "leave":
        elements.append({"tag": "markdown", "content": "🏖 <font color='grey'>本周期请假豁免，无需填报。</font>"})
    else:
        # 规范问题清单
        if spec["issues"]:
            elements.append({"tag": "markdown", "content": "<font color='#1F2329'>**📋 待完善项**</font>"})
            for it in spec["issues"][:5]:
                elements.append({"tag": "markdown", "content": f"　• {_esc(it)}"})
        else:
            elements.append({"tag": "markdown", "content": "✅ <font color='#34C724'>填报规范，很好！</font>"})
        # 本周期 todo 清单
        if g["todos"]:
            elements.append({"tag": "markdown", "content": "<font color='#1F2329'>**📝 本周期 TODO**</font>"})
            for t in g["todos"]:
                elements.append({"tag": "markdown", "content": f"　☐ {_esc(t)}"})

    if todo_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "去修改 TODO LIST"},
                         "type": "primary", "url": todo_url})

    # 反馈按钮（组长「提醒 TA」发来的卡带此区）
    if with_feedback:
        if responded:
            elements.append({"tag": "markdown",
                             "content": ("✅ <font color='#34C724'>**已反馈：已按要求修正**</font>"
                                         if responded == "fixed"
                                         else "📨 <font color='#3370FF'>**已反馈：收到**</font>")})
        else:
            elements.append({"tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                             "columns": [
                                 {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                                     "tag": "button", "text": {"tag": "plain_text", "content": "📨 已收到"},
                                     "type": "default",
                                     "behaviors": [{"type": "callback",
                                                    "value": {"action": "member_ack", "member_name": name,
                                                              "mentor_name": from_mentor, "cycle": g["latest"]}}]}]},
                                 {"tag": "column", "width": "weighted", "weight": 1, "elements": [{
                                     "tag": "button", "text": {"tag": "plain_text", "content": "✅ 已按要求修正"},
                                     "type": "primary",
                                     "behaviors": [{"type": "callback",
                                                    "value": {"action": "member_fixed", "member_name": name,
                                                              "mentor_name": from_mentor, "cycle": g["latest"]}}]}]}]})

    elements.append({"tag": "markdown", "content": (
        "<font color='#B0B6BF'>规范依据：大目标/小目标必填 标题+截止；todo 必填 标题+截止+验收人。"
        "机器初判，以人工为准 · 海豚三号</font>")})
    return {"schema": "2.0", "config": {"width_mode": "regular"},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"📌 我的 TODO · {name} · {g['latest']}"},
                       "template": template},
            "body": {"elements": elements}}


def _parse_action(card_action_json: str) -> dict:
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


async def feishu_member_todo_card(
    receive_id: str = "",
    member_name: str = "",
    todo_list_url: str = "",
    receive_id_type: str = "open_id",
    from_mentor: str = "",
    with_feedback: bool = False,
    card_action_json: str = "",
    user_key: str = "",
    agent_root: str = "",
) -> str:
    """组员个人 TODO 卡：填了没 + 规范 + 本周期 todo 清单 + 去修改 + 可选反馈按钮。

    **发卡**（传 receive_id + member_name）：读该组员本周期填报，判填报状态与规范，
    渲染 v9 六区数字格卡片私聊发给组员。``from_mentor`` 非空时卡头标注「<组长>提醒你
    检查」；``with_feedback=True`` 时底部加「📨 已收到」「✅ 已按要求修正」反馈按钮
    （组长「提醒 TA」发来的卡即走此路径）。时间戳=当前生成时刻。

    **处理反馈点击**（传 card_action_json）：action=``member_ack``/``member_fixed`` 时，
    记录该组员已响应（存 AppData ``member-feedback/``），edit_card 把组员卡更新为
    已反馈态。回流给组长由在线 runner 据此刷新组长卡对应行（responded）。

    Args:
        receive_id: 组员本人 open_id（发卡时必填）。
        member_name: 组员姓名（定位其填报）。
        todo_list_url: TODO LIST 链接（「去修改」按钮）。
        receive_id_type: 收件人 id 类型（默认 open_id）。
        from_mentor: 来源组长名（组长「提醒 TA」时带，卡头标注）。
        with_feedback: 是否加反馈按钮（组长发来的卡为 True）。
        card_action_json: 反馈点击 payload（处理点击时传）。
        user_key: 调用者/点击者 open_id。
        agent_root: workspace 根（留空自动解析）。

    Returns:
        JSON：ok / action(sent|feedback) / message_id / status / error。
    """
    # ── 处理组员反馈点击 ────────────────────────────────────────────────
    if card_action_json.strip():
        act = _parse_action(card_action_json)
        action = act.get("action") or ""
        if action not in ("member_ack", "member_fixed"):
            return json.dumps({"ok": False, "error": "unrecognized member action"}, ensure_ascii=False)
        m_name = act.get("member_name") or member_name
        mentor = act.get("mentor_name") or from_mentor
        cycle = act.get("cycle") or ""
        msg_id = act.get("_message_id") or ""
        resp = "fixed" if action == "member_fixed" else "ack"
        try:
            import anyio  # noqa: PLC0415
            from psi_agent._appdata import resolve_appdata_root  # noqa: PLC0415
            root = await resolve_appdata_root("")
            d = anyio.Path(root) / "member-feedback"
            await d.mkdir(parents=True, exist_ok=True)
            slug = f"{mentor}_{m_name}_{cycle}".replace("/", "_").replace("\\", "_")
            await (d / f"{slug}.json").write_text(json.dumps(
                {"mentor": mentor, "member": m_name, "cycle": cycle, "responded": resp,
                 "at": _now()}, ensure_ascii=False), encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            return json.dumps({"ok": False, "error": f"save feedback failed: {e!r}"}, ensure_ascii=False)
        if msg_id:
            try:
                g = _gather(agent_root, m_name)
                card = _build_member_card(g, todo_list_url.strip(), from_mentor=mentor,
                                          with_feedback=True, responded=resp)
                await _core.edit_card_impl(msg_id, json.dumps(card, ensure_ascii=False), user_key)
            except Exception as e:  # noqa: BLE001
                return json.dumps({"ok": True, "action": "feedback",
                                   "warn": f"saved but card update failed: {e!r}",
                                   "member": m_name, "responded": resp}, ensure_ascii=False)
        return json.dumps({"ok": True, "action": "feedback", "member": m_name,
                           "mentor": mentor, "responded": resp}, ensure_ascii=False)

    # ── 发卡 ────────────────────────────────────────────────────────────
    if not receive_id.strip() or not member_name.strip():
        return json.dumps({"ok": False, "error": "receive_id and member_name are required"},
                          ensure_ascii=False)
    try:
        g = _gather(agent_root, member_name.strip())
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"gather failed: {e!r}"}, ensure_ascii=False)
    if not g.get("found"):
        return json.dumps({"ok": False, "error": f"member {member_name} not found in todo_list_parsed"},
                          ensure_ascii=False)

    card = _build_member_card(g, todo_list_url.strip(), from_mentor=from_mentor.strip(),
                              with_feedback=with_feedback)
    handlers = (json.dumps({"member_ack": "feishu_member_todo_card",
                            "member_fixed": "feishu_member_todo_card"}, ensure_ascii=False)
                if with_feedback else "{}")
    res = await _core.send_card_impl(
        receive_id=receive_id.strip(),
        card_json=json.dumps(card, ensure_ascii=False),
        receive_id_type=receive_id_type.strip() or "open_id",
        user_key=user_key,
        business_context_json=json.dumps(
            {"kind": "member_todo_card", "member": member_name, "cycle": g["latest"]},
            ensure_ascii=False),
        action_handlers_json=handlers,
        multi_use=with_feedback,
    )
    if not res.get("ok"):
        return json.dumps({"ok": False, "error": res.get("message") or res.get("error") or res},
                          ensure_ascii=False, default=str)
    return json.dumps({"ok": True, "action": "sent", "message_id": res.get("message_id"),
                       "status": g["status"], "spec_level": g["spec"]["level"],
                       "issue_count": len(g["spec"]["issues"])},
                      ensure_ascii=False, default=str)
