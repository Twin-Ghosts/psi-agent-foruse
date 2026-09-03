# ruff: noqa: RUF001, RUF002, RUF003  # 中文全角标点是刻意排版
"""TODO 填报催办卡（周 1/3/5 前提醒未填报的人）。

把「谁还没填 todo list」从纯文本升级成 v9 六区数字格卡片：三列大数字
（已填 / 应填 / 未填）+ 未填点名区 + 请假豁免行 + 「去填写」按钮。

数据源：``todo_list_parsed.json``（date_cols + people[{name,mentor,cols}]），
填报状态判定复用 ``build_cards.member_status``（唯一口径：leave/filled/
not_joined/unfilled）。台账/考勤无关，纯填报状态，确定性、无需大模型。

两种发法：
- mode="group"：一张总览发到群/管理者，含未填点名（公开施压）。
- mode="dm"：只发给未填本人，文案对本人，不点名别人（温和）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import _feishu_impl as _core
from _runtime_paths import agent_dir

_NUM_FONT = "26px"
_TCOLOR = {"green": "#34C724", "blue": "#3370FF", "orange": "#FA8C16",
           "red": "#F53F3F", "default": "default"}
_SIGN = "海豚三号"


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    """卡片生成时刻（本地时间，MM-DD HH:MM）——保证卡面时间永远是最新的。"""
    import datetime  # noqa: PLC0415
    return datetime.datetime.now().strftime("%m-%d %H:%M")


def _tile(n: str, lab: str, color: str = "default") -> dict:
    """一个灰底数字格：大号着色数字 + 灰标签。"""
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


def _sign_footer() -> dict:
    return {"tag": "markdown", "content": f"<font color='#75726F'>{_SIGN}</font>"}


def _load_people(agent_root: str) -> tuple[list[str], list[dict]]:
    """读 todo_list_parsed.json → (date_cols, people)。复用 build_cards 数据层。"""
    mc = Path(agent_dir(agent_root)) / "mentor-cards"
    if str(mc) not in sys.path:
        sys.path.insert(0, str(mc))
    import build_cards as B  # noqa: PLC0415
    return B.load_people(None)


def _classify(date_cols: list[str], people: list[dict]) -> dict:
    """按 member_status 分类本周期填报状态，返回各名单与计数。"""
    mc_on_path = [p for p in sys.path if p.endswith("mentor-cards")]
    if not mc_on_path:
        raise RuntimeError("build_cards not on path; call _load_people first")
    import build_cards as B  # noqa: PLC0415

    latest = date_cols[-1]
    cycle_no = len(date_cols)
    leave_window, _att = B.runtime_windows(latest)
    exempt = B.leave_exempt_names(leave_window)
    join_map = B.join_dates()
    cd = B._cycle_date(latest)

    filled: list[str] = []
    unfilled: list[str] = []
    leave: list[str] = []
    for p in people:
        st = B.member_status(p, latest, exempt, join_map, cd)
        if st == "filled":
            filled.append(p["name"])
        elif st == "unfilled":
            unfilled.append(p["name"])
        elif st == "leave":
            leave.append(p["name"])
        # not_joined 不计应填、不点名
    expected = len(filled) + len(unfilled)  # 应填 = 已填 + 未填（不含请假/未入职）
    return {
        "latest": latest, "cycle_no": cycle_no,
        "filled": filled, "unfilled": unfilled, "leave": leave,
        "expected": expected, "as_of": B.data_as_of(),
    }


def _build_group_card(c: dict, todo_url: str) -> dict:
    """群发总览：三列大数字 + 未填点名 + 请假豁免 + 去填写按钮。"""
    filled_n, expected, unfilled_n = len(c["filled"]), c["expected"], len(c["unfilled"])
    template = "green" if unfilled_n == 0 else "red"
    head_num = "green" if unfilled_n == 0 else "red"
    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"<font color='#8F959E'>🕐 {_now()} 生成 · 数据截至 {c['as_of']} · 截止今天 15:00</font>"},
        {"tag": "hr"},
        _sec("👥 填报进度"),
        _tile_row3(
            _tile(f"{filled_n}/{expected}", "已填报", "green"),
            _tile(str(expected), "应填报", "blue"),
            _tile(str(unfilled_n), "未填报", head_num)),
    ]
    if c["unfilled"]:
        names = " · ".join(_esc(n) for n in c["unfilled"])
        elements.append({"tag": "markdown",
                         "content": f"⚠️ <font color='#F53F3F'>还没填的同学（{unfilled_n}）：</font>{names}"})
    else:
        elements.append({"tag": "markdown",
                         "content": "✅ <font color='#34C724'>本周期全员已填报，辛苦大家！</font>"})
    if c["leave"]:
        lv = " · ".join(_esc(n) for n in c["leave"])
        elements.append({"tag": "markdown",
                         "content": f"🏖 <font color='grey'>请假豁免（{len(c['leave'])}）：{lv}</font>"})
    if todo_url:
        elements.append({"tag": "hr"})
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "去填写 TODO LIST"},
                         "type": "primary", "url": todo_url})
    elements.append(_sign_footer())
    return {"schema": "2.0", "config": {"width_mode": "regular"},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"📝 TODO 填报提醒 · {c['latest']} 第{c['cycle_no']}周期"},
                       "template": template},
            "body": {"elements": elements}}


def _build_dm_card(name: str, c: dict, todo_url: str) -> dict:
    """私聊本人：只对本人说，不点名别人。"""
    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"<font color='#1F2329'>**{_esc(name)}**，你本周期（{c['latest']}）还没填写 TODO LIST。</font>"},
        {"tag": "markdown",
         "content": "<font color='#F53F3F'>截止今天 15:00</font>，请尽快补上，避免报表标记「未按时」。"},
    ]
    if todo_url:
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "去填写 TODO LIST"},
                         "type": "primary", "url": todo_url})
    elements.append(_sign_footer())
    return {"schema": "2.0", "config": {"width_mode": "regular"},
            "header": {"title": {"tag": "plain_text", "content": f"📝 TODO 填报提醒 · {c['latest']}"},
                       "template": "red"},
            "body": {"elements": elements}}


def _sec(title: str) -> dict:
    return {"tag": "markdown", "content": f"<font color='#1F2329'>**{title}**</font>"}


async def feishu_todo_fill_reminder(
    receive_id: str,
    mode: str = "group",
    todo_list_url: str = "",
    receive_id_type: str = "open_id",
    dm_name: str = "",
    user_key: str = "",
    agent_root: str = "",
) -> str:
    """发送「TODO 填报催办卡」——提醒本周期还没填 todo list 的人。

    读 ``todo_list_parsed.json``，用 ``build_cards.member_status`` 判本周期填报
    状态（唯一口径），渲染成 v9 六区数字格卡片：三列大数字（已填/应填/未填）
    + 未填点名区 + 请假豁免行 + 「去填写」按钮。纯只读、无回调。

    Args:
        receive_id: 收件人 id（group 模式=群/管理者；dm 模式=未填本人）。
        mode: "group"（群发总览，含未填点名）或 "dm"（私聊本人，不点名别人）。
        todo_list_url: TODO LIST 表格链接，渲染「去填写」按钮；空则不渲染按钮。
        receive_id_type: 收件人 id 类型（open_id / chat_id / user_id …）。
        dm_name: mode="dm" 时收件人的姓名（卡面称呼）；group 模式忽略。
        user_key: 调用者 open_id（发送身份回退用）。
        agent_root: workspace 根（留空自动解析）。

    Returns:
        JSON：ok / mode / message_id / counts（filled/expected/unfilled）/
        unfilled（未填名单，group 模式）/ error。
    """
    mode = (mode or "group").strip()
    if mode not in ("group", "dm"):
        return json.dumps({"ok": False, "error": "mode must be 'group' or 'dm'"}, ensure_ascii=False)
    if not receive_id.strip():
        return json.dumps({"ok": False, "error": "receive_id is required"}, ensure_ascii=False)

    try:
        date_cols, people = _load_people(agent_root)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"load todo_list_parsed failed: {e!r}"}, ensure_ascii=False)
    if not date_cols:
        return json.dumps({"ok": False, "error": "no cycle columns in todo_list_parsed.json"}, ensure_ascii=False)

    c = _classify(date_cols, people)

    if mode == "dm":
        card = _build_dm_card(dm_name.strip() or "同学", c, todo_list_url.strip())
    else:
        card = _build_group_card(c, todo_list_url.strip())

    res = await _core.send_card_impl(
        receive_id=receive_id.strip(),
        card_json=json.dumps(card, ensure_ascii=False),
        receive_id_type=receive_id_type.strip() or "open_id",
        user_key=user_key,
        business_context_json=json.dumps(
            {"kind": "todo_fill_reminder", "mode": mode, "cycle": c["latest"]}, ensure_ascii=False),
        action_handlers_json="{}",
    )
    if not res.get("ok"):
        return json.dumps({"ok": False, "error": res.get("message") or res.get("error") or res},
                          ensure_ascii=False, default=str)
    out = {
        "ok": True, "mode": mode, "message_id": res.get("message_id"),
        "counts": {"filled": len(c["filled"]), "expected": c["expected"],
                   "unfilled": len(c["unfilled"]), "leave": len(c["leave"])},
    }
    if mode == "group":
        out["unfilled"] = c["unfilled"]
    return json.dumps(out, ensure_ascii=False, default=str)
