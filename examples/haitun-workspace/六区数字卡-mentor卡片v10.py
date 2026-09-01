#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mentor 周期报表卡 v10（2026-08-29 · 马晨柯需求「mentor卡片用v10，总览用v9」）。

在 v9 mentor 卡（六区数字 + 链接，无逐人）基础上，套用此前全公司总览 v10 的版式：
  1. 灰底三行 9 个核心数字（① 填报/在册/未按时；② 已闭环/进行中/逾期；③ 请假/考勤异常/平均分）
     ——充分但都是重要项；
  2. 一句话结论（填报、未按时名单、逾期、请假豁免、考勤异常 + 趋势描述）；
  3. 组内全员逐行一览：状态四选一（✅⚠️🏖📅）+ 姓名 + 考勤异常/名下逾期红字标记，
     按「未按时 → 异常 → 请假 → 未入职 → 正常」排序，mentor 一眼扫完谁要催；
  4. 明细一律走链接（台账/任务/逾期/请假/考勤/趋势），卡片内不放表格。

总览卡（全公司）保持 v9 样式 → 六区数字卡-todo总览.py（委托 build_boss_v6）不动。

数据全部实时推导（复用 build_cards 数据通道 + v9 指标计算），无死数据。

产出：
    mentor_cards/mentor_cards_v10.json   8 张 mentor 卡（发卡用）
    六区数字卡-mentor卡片v10.json        孙逊组卡（单独文件，发卡/预览用）
"""
from __future__ import annotations

import importlib.util
import json
import sys
from collections import defaultdict
from pathlib import Path

WS = Path(__file__).resolve().parent
sys.path.insert(0, str(WS / "mentor-cards"))

# 加载 v9 脚本（文件名含中文/连字符，用 importlib）
_V9 = importlib.util.spec_from_file_location(
    "v9cards", WS / "六区数字卡-真实卡片v9.py")
V9 = importlib.util.module_from_spec(_V9)
sys.modules["v9cards"] = V9
_V9.loader.exec_module(V9)

import build_cards as B  # noqa: E402

STATUS_ICON = {"filled": "✅", "unfilled": "⚠️", "leave": "🏖", "not_joined": "📅"}
STATUS_COLOR = {"filled": "#34C724", "unfilled": "#F53F3F",
                "leave": "#8F959E", "not_joined": "#8F959E"}
RED = "#F53F3F"


def _row_rank(r):
    """组内排序：未按时(0) → 考勤异常/逾期标记(1) → 请假(2) → 未入职(3) → 正常(4)。"""
    st, line = r
    if st == "unfilled":
        return 0
    if RED in line:
        return 1
    if st == "leave":
        return 2
    if st == "not_joined":
        return 3
    return 4


def build_mentor_v10(mentor: str, members: list[dict], latest: str,
                     date_cols: list[str], exempt: set[str],
                     join_map: dict[str, str]) -> dict:
    m = V9.compute_mentor_metrics(mentor, members, latest, date_cols, exempt, join_map)
    cycle_no = len(date_cols)
    total, filled, unfilled = m["total"], m["filled"], m["unfilled"]
    closure = m["closure"]  # (已闭环, 进行中, …, 逾期)
    overdue_cnt = m["overdue_cnt"]
    g_leaves = m["g_leaves"]
    anomalies = m["anomalies"]
    eval_ = m["eval"]
    desc, cur_pct = m["desc"], m["cur_pct"]
    ledger_src, ledger_url = m["ledger_src"], m["ledger_url"]

    cd = B._cycle_date(latest)
    unfilled_names = [p["name"] for p in members
                      if B.member_status(p, latest, exempt, join_map, cd) == "unfilled"]

    lv_url, od_url, att_url = V9.mentor_links(mentor)
    closed_url, doing_url = V9.mentor_task_links(mentor)
    trend_url = V9.trend_url_of(mentor)

    unfilled_col = "red" if unfilled else "green"
    overdue_col = "red" if overdue_cnt else "green"
    att_col = "red" if anomalies else "green"
    leave_col = "orange" if g_leaves else "green"
    doing_col = "orange" if closure[1] else "green"

    # ── 组内成员逐行一览（状态四选一 + 红字异常标记） ──
    att = anomalies
    overdue_by_owner: dict[str, int] = defaultdict(int)
    for owner, _task, _mark in m["overdue"]:
        overdue_by_owner[owner] += 1

    lines: list[tuple[str, str]] = []
    for p in members:
        st = B.member_status(p, latest, exempt, join_map, cd)
        icon = STATUS_ICON[st]
        color = STATUS_COLOR[st]
        flags = []
        if p["name"] in att:
            flags.append(f"<font color='{RED}'>{'·'.join(att[p['name']])}</font>")
        if overdue_by_owner.get(p["name"]):
            flags.append(f"<font color='{RED}'>逾期{overdue_by_owner[p['name']]}</font>")
        extra = ("　" + "　".join(flags)) if flags else ""
        lines.append((st, f"<font color='{color}'>{icon}</font> **{B.esc(p['name'])}**{extra}"))
    lines.sort(key=_row_rank)

    info = (f"🗂 {ledger_src}　🕐 数据截至 {m['as_of']}　"
            f"👥 组内 {total} 人 · 状态四选一 ✅⚠️🏖📅")

    # 灰底数字区（9 个核心）
    elements = [
        {"tag": "markdown", "content": f"<font color='#8F959E'>{info}</font>"},
        {"tag": "hr"},
        V9.sec("👥 ① 人员与填报"),
        V9.tile_row3(
            V9.tile(f"{filled}/{total}", "填报", "green"),
            V9.tile(str(total), "在册", "blue"),
            V9.tile(str(unfilled), "未按时", unfilled_col),
        ),
        V9.sec("📊 ② 任务与台账", ledger_src),
        V9.tile_row3(
            V9.tile(str(closure[0]), "已闭环", "green"),
            V9.tile(str(closure[1]), "进行中", doing_col),
            V9.tile(str(overdue_cnt), "逾期", overdue_col),
        ),
        V9.sec("🏖 ③ 请假与考勤"),
        V9.tile_row3(
            V9.tile(str(len(g_leaves)), "请假", leave_col),
            V9.tile(str(len(anomalies)), "考勤异常", att_col),
            V9.tile(f"{eval_.get('avg', '-')}★", "平均分", "orange"),
        ),
        {"tag": "hr"},
    ]

    # 一句话结论
    parts = [f"填报 {filled}/{total}"]
    parts.append("无未按时" if not unfilled
                 else f"未按时 {unfilled} 人（{'、'.join(unfilled_names)}）")
    if overdue_cnt:
        parts.append(f"逾期 {overdue_cnt} 条待催")
    if g_leaves:
        parts.append(f"请假豁免 {len(g_leaves)} 人")
    if anomalies:
        parts.append(f"考勤异常 {len(anomalies)} 人")
    summary = "📌 " + " · ".join(parts) + f"　趋势 **{desc}**"
    elements.append({"tag": "markdown", "content": summary})

    # 组内一览
    elements.append(V9.sec(f"👥 组内一览（{total} 人）"))
    for st, line in lines:
        elements.append({"tag": "markdown", "content": line})

    # 明细链接区
    elements.append({"tag": "hr"})
    if ledger_url:
        elements.append({"tag": "markdown",
                         "content": f"🗂 台账总览（{ledger_src}）　"
                                    f"<font color='grey'>[打开明细 →]({ledger_url})</font>"})
    closed_rows = V9.task_rows(m["rows"], "已交付")
    doing_rows = V9.task_rows(m["rows"], "进行中")
    if closed_rows:
        elements.append({"tag": "markdown",
                         "content": f"✅ 已闭环任务（{len(closed_rows)}）　"
                                    f"<font color='grey'>[明细 →]({closed_url})</font>"}
                        if closed_url else
                        {"tag": "markdown",
                         "content": f"✅ 已闭环任务（{len(closed_rows)}）"})
    if doing_rows:
        elements.append({"tag": "markdown",
                         "content": f"🔄 进行中任务（{len(doing_rows)}）　"
                                    f"<font color='grey'>[明细 →]({doing_url})</font>"}
                        if doing_url else
                        {"tag": "markdown",
                         "content": f"🔄 进行中任务（{len(doing_rows)}）"})
    if od_url:
        elements.append({"tag": "markdown",
                         "content": f"⚠️ 逾期明细（{overdue_cnt} 条）　"
                                    f"<font color='grey'>[明细 →]({od_url})</font>"})
    if lv_url:
        elements.append({"tag": "markdown",
                         "content": f"🏖 请假标注（{len(g_leaves)} 人）　"
                                    f"<font color='grey'>[名单 →]({lv_url})</font>"})
    if att_url:
        elements.append({"tag": "markdown",
                         "content": f"⏰ 考勤异常（{m['att_window']} · {len(anomalies)} 人）　"
                                    f"<font color='grey'>[异常明细 →]({att_url})</font>"})
    if trend_url:
        elements.append({"tag": "markdown",
                         "content": f"📈 填报趋势（近 {B.TREND_WINDOW} 期 · 非请假口径）　"
                                    f"<font color='grey'>[折线图 →]({trend_url})</font>"})
    else:
        elements.append({"tag": "markdown",
                         "content": "📈 填报趋势　<font color='grey'>（折线图生成中）</font>"})

    # 底部按钮
    elements.append({
        "tag": "column_set", "flex_mode": "none", "horizontal_spacing": "8px",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "📊 打开台账"},
                 "type": "primary", "url": ledger_url}]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [
                {"tag": "button",
                 "text": {"tag": "plain_text", "content": "📋 打开 TODO 总表"},
                 "type": "primary", "url": V9.TODO_LIST_URL}]},
        ],
    })

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


def main() -> int:
    date_cols, people = B.load_people(None)
    if not date_cols:
        print("[err] 没有周期列（TODO LIST 数据源为空）", file=sys.stderr)
        return 1
    latest = date_cols[-1]
    leave_window, att_window = B.runtime_windows(latest)
    exempt = B.leave_exempt_names(leave_window)
    join_map = B.join_dates()
    print(f"[data] 最新周期 {latest}（第 {len(date_cols)} 周期）· 数据截至 "
          f"{B.data_as_of()} · 考勤窗口 {att_window} · 豁免 {len(exempt)} 人")

    by_mentor: dict[str, list[dict]] = defaultdict(list)
    for p in people:
        by_mentor[p["mentor"] or "(未分组)"].append(p)
    mentor_names = [m for m in by_mentor if m != "(未分组)"]
    oids = B.mentor_oids(mentor_names)

    out: dict[str, dict] = {}
    for mentor in mentor_names:
        members = by_mentor.get(mentor, [])
        oid = oids.get(mentor, "")
        if not oid:
            print(f"[warn] mentor {mentor} 无 open_id，跳过", file=sys.stderr)
            continue
        card = build_mentor_v10(mentor, members, latest, date_cols, exempt, join_map)
        out[mentor] = {"oid": oid, "card": card}
        print(f"[mentor] {mentor}（{len(members)}人）卡片完成 · "
              f"template={card['header']['template']} · "
              f"{len(card['body']['elements'])} 元素")

    dest = WS / "mentor-cards" / "mentor_cards_v10.json"
    dest.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[ok] 全部卡片 → {dest}")

    if "孙逊" in out:
        sun = WS / "六区数字卡-mentor卡片v10.json"
        sun.write_text(json.dumps(out["孙逊"]["card"], ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"[ok] 孙逊组卡 → {sun}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
