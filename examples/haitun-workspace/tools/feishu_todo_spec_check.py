# ruff: noqa: RUF001, RUF002, RUF003  # 中文全角标点是刻意排版
"""TODO 填报规范体检卡（检查大家 todo list 写得规不规范）。

把「谁写得不规范」从纯文本升级成 v9 六区数字格卡片：三列大数字
（合规 / 待完善 / 缺必填）+ 按人分组的问题清单 + 每条「已修正」按钮。

规范口径（对齐 company-todo-sync 的字段规则）：
- 大目标：必填 标题 + 截止日期；选填 友商对比 / 外部成果
- 小目标：必填 标题 + 截止日期
- todo  ：必填 标题 + 截止日期 + 验收人

判定用启发式规则（确定性、无需大模型）：
- 截止日期：文本含 (MM-DD) / （MM-DD） / MM-DD / MM.DD / 12-31 等 → 有
- 验收人  ：todo 段含「验收人/A:/负责人/@某」等关键词 → 有
- 分级    ：缺必填=红(缺必填)；仅缺选填=橙(待完善)；齐全=绿(合规)

解析对不规则写法可能漏判/误判——卡面注明「机器初判，以人工为准」。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import _feishu_impl as _core
from _runtime_paths import agent_dir

_NUM_FONT = "26px"
_SIGN = "海豚三号"

# 截止日期：MM-DD / MM.DD / M月D日 / （MM-DD） / 单独 N月 / 年底/季度末 等时间表述
_DUE_RE = re.compile(
    r"[（(]?\d{1,2}\s*[-.月/]\s*\d{1,2}\s*[日号)）]?"  # 8-29 / 8.29 / 8月29日
    r"|\d{1,2}\s*月(?:底|初|中)?"                       # 12月 / 12月底
    r"|年底|年终|季度末|月底|本周|下周|近期(?=前|完成|交付)")
# 验收人关键词（todo 必填）：验收人 / 验收 / 由X验收 / X验收 / 负责人 / 交付给 / A:/T: / @人
_ACCEPTER_RE = re.compile(r"验收人|验收|负责人|交付[给对]|由\S+?验收|[AaTt]\s*[:：]|@\S")
# 三级层级标题行
_BIG_RE = re.compile(r"^\s*(?:大目标|目标)\s*[一二三四1234]?\s*[:：]?")
_SMALL_RE = re.compile(r"^\s*(?:小目标|子目标)\s*[一二三四1234]?\s*[:：]?")
_TODO_RE = re.compile(r"^\s*(?:todo|TODO|待办|任务)\s*[一二三四1234]?\s*[:：]?", re.IGNORECASE)


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> str:
    """卡片生成时刻（本地时间，MM-DD HH:MM）——保证卡面时间永远是最新的。"""
    import datetime  # noqa: PLC0415
    return datetime.datetime.now().strftime("%m-%d %H:%M")


def _has_due(text: str) -> bool:
    return bool(_DUE_RE.search(text))


def _split_sections(raw: str) -> dict[str, str]:
    """把整段填报按「大目标/小目标/todo」三级粗切成三块文本。"""
    lines = raw.splitlines()
    buckets = {"big": [], "small": [], "todo": []}
    cur = None
    for ln in lines:
        if _BIG_RE.match(ln):
            cur = "big"
        elif _SMALL_RE.match(ln):
            cur = "small"
        elif _TODO_RE.match(ln):
            cur = "todo"
        if cur:
            buckets[cur].append(ln)
    return {k: "\n".join(v) for k, v in buckets.items()}


def _check_person(name: str, raw: str) -> dict:
    """检查一个人的填报，返回 {name, level(green/orange/red), issues:[...]}。"""
    text = (raw or "").strip()
    if not text:
        return {"name": name, "level": "red", "issues": ["未填报任何内容"]}

    sec = _split_sections(text)
    issues_required: list[str] = []  # 缺必填 → 红
    issues_optional: list[str] = []  # 缺选填 → 橙

    # 大目标：必填 标题+截止；选填 友商对比/外部成果
    if sec["big"].strip():
        if not _has_due(sec["big"]):
            issues_required.append("大目标缺「截止日期」")
        if "友商" not in sec["big"] and "对比" not in sec["big"]:
            issues_optional.append("大目标缺「友商对比」")
        if "外部成果" not in sec["big"] and "用户数" not in sec["big"] and "金额" not in sec["big"]:
            issues_optional.append("大目标缺「外部成果」")
    else:
        issues_required.append("缺「大目标」段")

    # 小目标：必填 标题+截止
    if sec["small"].strip() and not _has_due(sec["small"]):
        issues_required.append("小目标缺「截止日期」")

    # todo：必填 标题+截止+验收人
    if sec["todo"].strip():
        if not _has_due(sec["todo"]):
            issues_required.append("todo 缺「截止日期」")
        if not _ACCEPTER_RE.search(sec["todo"]):
            issues_required.append("todo 缺「验收人」")
    else:
        issues_optional.append("未列具体 todo")

    if issues_required:
        level = "red"
    elif issues_optional:
        level = "orange"
    else:
        level = "green"
    return {"name": name, "level": level, "issues": issues_required + issues_optional}


# 卡片构造与主函数在下方追加


_TCOLOR = {"green": "#34C724", "orange": "#FA8C16", "red": "#F53F3F", "default": "default"}
_LEVEL_ICON = {"red": "🔴", "orange": "🟠", "green": "🟢"}
_MAX_LIST = 8  # 单屏最多列几条问题，超出截断


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


def _build_spec_card(cycle: str, cycle_no: int, results: list[dict],
                     as_of: str, list_url: str = "", with_button: bool = True) -> dict:
    """规范体检卡：三列大数字 + 按人问题清单（每条带「已修正」按钮）。"""
    green = sum(1 for r in results if r["level"] == "green")
    orange = sum(1 for r in results if r["level"] == "orange")
    red = sum(1 for r in results if r["level"] == "red")
    checked = len(results)
    template = "green" if (red == 0 and orange == 0) else ("red" if red else "orange")

    elements: list[dict] = [
        {"tag": "markdown",
         "content": f"<font color='#8F959E'>🕐 {_now()} 生成 · 已检查 {checked} 份填报 · 数据截至 {as_of}</font>"},
        {"tag": "hr"},
        _tile_row3(
            _tile(str(green), "合规", "green"),
            _tile(str(orange), "待完善", "orange"),
            _tile(str(red), "缺必填", "red")),
    ]

    problems = [r for r in results if r["issues"]]
    problems.sort(key=lambda r: 0 if r["level"] == "red" else 1)  # 红在前
    if problems:
        elements.append({"tag": "markdown",
                         "content": "<font color='#1F2329'>**📋 规范问题清单（按人）**</font>"})
        for r in problems[:_MAX_LIST]:
            icon = _LEVEL_ICON.get(r["level"], "🟠")
            issue_txt = " / ".join(_esc(i) for i in r["issues"][:3])
            line = {"tag": "markdown", "content": f"{icon} **{_esc(r['name'])}** — {issue_txt}"}
            if with_button:
                elements.append({
                    "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
                    "columns": [
                        {"tag": "column", "width": "weighted", "weight": 5,
                         "vertical_align": "center", "elements": [line]},
                        {"tag": "column", "width": "weighted", "weight": 1,
                         "vertical_align": "center", "elements": [{
                             "tag": "button", "size": "tiny",
                             "text": {"tag": "plain_text", "content": "已修正"},
                             "type": "default",
                             "behaviors": [{"type": "callback",
                                            "value": {"action": "spec_recheck",
                                                      "name": r["name"], "cycle": cycle}}]}]}],
                })
            else:
                elements.append(line)
        if len(problems) > _MAX_LIST:
            more = len(problems) - _MAX_LIST
            tail = f"<font color='grey'>… 另有 {more} 人存在问题"
            tail += f"，[查看完整清单 →]({list_url})</font>" if list_url else "</font>"
            elements.append({"tag": "markdown", "content": tail})
    else:
        elements.append({"tag": "markdown",
                         "content": "✅ <font color='#34C724'>本周期填报全部合规，很棒！</font>"})

    if list_url:
        elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "去修改 TODO LIST"},
                         "type": "primary", "url": list_url})
    elements.append({"tag": "hr"})
    elements.append({"tag": "markdown", "content": (
        "<font color='#B0B6BF'>规范依据：大目标/小目标必填 标题+截止；todo 必填 标题+截止+验收人。"
        "本清单为机器初判，以人工复核为准 · 海豚三号</font>")})
    return {"schema": "2.0", "config": {"width_mode": "regular"},
            "header": {"title": {"tag": "plain_text",
                                 "content": f"🔍 TODO 规范体检 · {cycle} 第{cycle_no}周期"},
                       "template": template},
            "body": {"elements": elements}}


def _load_and_check(agent_root: str) -> tuple[str, int, list[dict], str]:
    """读 todo_list_parsed.json，对本周期每份填报做规范检查。"""
    mc = Path(agent_dir(agent_root)) / "mentor-cards"
    if str(mc) not in sys.path:
        sys.path.insert(0, str(mc))
    import build_cards as B  # noqa: PLC0415
    date_cols, people = B.load_people(None)
    if not date_cols:
        return "", 0, [], ""
    latest = date_cols[-1]
    results = [_check_person(p["name"], p["cols"].get(latest) or "")
               for p in people if (p["cols"].get(latest) or "").strip()]
    return latest, len(date_cols), results, B.data_as_of()


async def feishu_todo_spec_check(
    receive_id: str,
    list_url: str = "",
    receive_id_type: str = "open_id",
    with_button: bool = True,
    only_name: str = "",
    user_key: str = "",
    agent_root: str = "",
) -> str:
    """发送「TODO 规范体检卡」——检查本周期填报是否规范。

    读 ``todo_list_parsed.json``，对每份填报按字段规则（大目标/小目标必填
    标题+截止；todo 必填 标题+截止+验收人）做启发式检查，三档分级
    （合规/待完善/缺必填），渲染成 v9 六区数字格卡片：三列大数字 + 按人
    问题清单，每条带「已修正」按钮（点击回调 ``spec_recheck``，可重新校验该人
    并原地更新卡片）。判定为机器初判，卡面注明以人工为准。

    Args:
        receive_id: 收件人 id（群/管理者 chat_id，或某人 open_id）。
        list_url: 完整清单链接（问题超一屏时「查看完整清单」指向它）。
        receive_id_type: 收件人 id 类型（open_id / chat_id / …）。
        with_button: 每条问题是否带「已修正」交互按钮（默认 True）。
        only_name: 只检查并展示某一个人（私聊本人场景）；空=全员。
        user_key: 调用者 open_id（发送身份回退用）。
        agent_root: workspace 根（留空自动解析）。

    Returns:
        JSON：ok / message_id / counts（green/orange/red/checked）/ error。
    """
    if not receive_id.strip():
        return json.dumps({"ok": False, "error": "receive_id is required"}, ensure_ascii=False)
    try:
        cycle, cycle_no, results, as_of = _load_and_check(agent_root)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"ok": False, "error": f"load/check failed: {e!r}"}, ensure_ascii=False)
    if not cycle:
        return json.dumps({"ok": False, "error": "no cycle columns in todo_list_parsed.json"},
                          ensure_ascii=False)
    if only_name.strip():
        results = [r for r in results if r["name"] == only_name.strip()]

    card = _build_spec_card(cycle, cycle_no, results, as_of, list_url.strip(), with_button)
    handlers = json.dumps({"spec_recheck": "feishu_todo_spec_check"}, ensure_ascii=False) if with_button else "{}"
    res = await _core.send_card_impl(
        receive_id=receive_id.strip(),
        card_json=json.dumps(card, ensure_ascii=False),
        receive_id_type=receive_id_type.strip() or "open_id",
        user_key=user_key,
        business_context_json=json.dumps(
            {"kind": "todo_spec_check", "cycle": cycle}, ensure_ascii=False),
        action_handlers_json=handlers,
        multi_use=with_button,
    )
    if not res.get("ok"):
        return json.dumps({"ok": False, "error": res.get("message") or res.get("error") or res},
                          ensure_ascii=False, default=str)
    green = sum(1 for r in results if r["level"] == "green")
    orange = sum(1 for r in results if r["level"] == "orange")
    red = sum(1 for r in results if r["level"] == "red")
    return json.dumps({"ok": True, "message_id": res.get("message_id"),
                       "counts": {"green": green, "orange": orange, "red": red,
                                  "checked": len(results)}},
                      ensure_ascii=False, default=str)
