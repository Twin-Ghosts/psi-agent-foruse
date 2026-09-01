#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""六区数字卡（真实数据版）生成器。

数据全部来自 mentor_cards 真实管线（build_cards.py，已修「纯请假标记格误计为
已填报」bug）：
  · ① 人员概况 / 考勤异常   ← TODO LIST 填报 + data/attendance.json（飞书考勤）
  · ② 目标数量 ③ 完成情况 ⑥ 评价概况 ← 台账 ledger_孙逊.json（孙逊真实台账，截至 08-17）
  · ④ 逾期 ← 台账 + 关键词自动扫描 + 人工核对档案（无 → 无逾期）
  · ⑤ 请假 ← data/leave.json（飞书审批已批准，近周期窗口）
  · 趋势图   ← trend_series()（近 8 期，分母 = 已入职 − 当日请假）

产出：六区数字卡-真实数据.html + .png（node playwright 渲染）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

WS = Path(__file__).resolve().parent
sys.path.insert(0, str(WS / "mentor_cards"))
import build_cards as B  # noqa: E402

MENTOR = "孙逊"
# TODO 总表链接由海豚运行时解析（data/sources.json），代码不硬编码；缺失时按钮降级
TODO_URL = B.load_todo_source().get("url", "")

# ---------------------------------------------------------------------------
# 1) 真实数据
# ---------------------------------------------------------------------------
date_cols, people = B.load_people(None)
g = [p for p in people if p.get("mentor") == MENTOR]
latest = date_cols[-1]
cd = B._cycle_date(latest)
cycle_no = len(date_cols)
as_of = B.data_as_of()
leave_window, att_window = B.runtime_windows(latest)
exempt = B.leave_exempt_names(leave_window)
join_map = B.join_dates()

total = len(g)
filled = sum(1 for p in g if B.member_status(p, latest, exempt, join_map, cd) == "filled")
unfilled = sum(1 for p in g if B.member_status(p, latest, exempt, join_map, cd) == "unfilled")
leave_cnt = len({e["name"] for e in B.group_leaves([p["name"] for p in g], leave_window)})

# 台账口径（孙逊真实台账已注册）
ledger = B.load_ledger(MENTOR)
rows = B.ledger_latest_rows(ledger)
goals = B.goal_counts_from_ledger(rows) or (0, 0, 0)
closure = B.closure_from_ledger(rows) or (0, 0, 0, 0, 0)
eval_ = B.evaluation_from_ledger(rows) or {}
ledger_src = f"台账·截至{ledger.get('latest_cycle', '')[-5:]}"

overdue = B.overdue_from_ledger(rows) + B.auto_overdue_extra(g, latest, B.overdue_from_ledger(rows), exempt, join_map)
overdue = B._visible_overdue(overdue, exempt)
overdue_cnt = len(overdue)

g_leaves = B.group_leaves([p["name"] for p in g], leave_window)
anomalies = B.attendance_anomalies({p["name"] for p in g})

# 趋势（近 8 期，用修正后的 _real_fill；有效 = 已入职 − 当日请假）
approvals = B.approved_leaves()

def on_leave(name: str, d: str) -> bool:
    return any((e.get("start") or "")[:10] <= d <= (e.get("end") or "")[:10]
               for e in approvals if e.get("name") == name)

trend = []  # (label, filled, eligible, leave, valid, pct, leave_filled)
for c in date_cols[-B.TREND_WINDOW:]:
    d = B._cycle_date(c)
    elig = [p for p in g if B.joined_by(join_map, p["name"], d)]
    lv_names = [p["name"] for p in elig if on_leave(p["name"], d)]
    fl = [p["name"] for p in elig if B._real_fill(p["cols"].get(c) or "")]
    lv_filled = [n for n in fl if n in lv_names]
    valid = len(elig) - len(lv_names)
    pct = len(fl) / valid * 100 if valid else 0.0
    trend.append((c, len(fl), len(elig), len(lv_names), valid, pct, len(lv_filled)))

tot_f = sum(t[1] for t in trend)
tot_v = sum(t[4] for t in trend)
tot_lf = sum(t[6] for t in trend)
avg_pct = tot_f / tot_v * 100 if tot_v else 0.0

print(f"[{MENTOR}] 总{total}人 填报{filled} 未按时{unfilled} 请假窗口内{leave_cnt}人 "
      f"考勤异常{len(anomalies)}")
print(f"[台账] {ledger_src} 目标{goals} 完成{closure} 逾期{overdue_cnt} 评价{n}条"
      if False else f"[台账] {ledger_src} 目标{goals} 完成{closure} 逾期{overdue_cnt} 评价{eval_.get('n')}条")
print("[趋势] " + " ".join(f"{t[0]}={t[1]}/{t[4]}@{t[5]:.0f}%" for t in trend))
print(f"[汇总] 填报{tot_f} 有效{tot_v} 填报率{avg_pct:.0f}% 在假仍填{tot_lf}")

# ---------------------------------------------------------------------------
# 2) HTML
# ---------------------------------------------------------------------------
def numcard(n: str, lab: str, cls: str = "blue") -> str:
    return f'<div class="numcard"><div class="n {cls}">{n}</div><div class="lab">{lab}</div></div>'

def lamp(dot: str, text: str, link: str = "#") -> str:
    return (f'<div class="lamp"><span class="dot {dot}"></span>'
            f'<span class="t">{text}</span>'
            f'<a class="go" href="{link}">{"详情 →" if link != "#" else "详情 →"}</a></div>')

# ① 考勤异常行
att_line = ""
if anomalies:
    shown = "、".join(f"{n}（{' · '.join(v)}）" for n, v in sorted(anomalies.items())[:4])
    att_line = f'<div class="att"><span>⏰ 考勤异常（{att_window}）：{shown}</span></div>'

# 趋势柱
bars = []
for c, f, t, lv, valid, pct, lf in trend:
    over = pct > 100
    h = min(pct, 100.0)
    pill_cls = "pill over" if over else "pill"
    pct_cls = "pct over" if over else "pct"
    pct_txt = f"{pct:.0f}%"
    bars.append(
        f'<div class="col">'
        f'<div class="{pct_cls}">{pct_txt}</div>'
        f'<div class="{pill_cls}" style="height:{h:.0f}px"></div>'
        f'<div class="x">{c}</div>'
        f'<div class="n">{f}/{valid}</div>'
        f'</div>')

# ⑥ 评价分布
dist = eval_.get("dist", {})
bucket = " ｜ ".join(f"{lab}×{n}" for lab, n in
                     (("5★", dist.get("5", 0)), ("4★", dist.get("4", 0)),
                      ("3★", dist.get("3", 0)), ("≤2★", dist.get("le2", 0))) if n)

html = f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{
    background:#EDEFF3;
    font-family:"Droid Sans Fallback","Noto Sans CJK SC","Microsoft YaHei",sans-serif;
    padding:36px 20px 60px;
    width:520px;
  }}
  .plan-tag {{
    font-size:12px; color:#666; font-weight:700;
    margin:0 0 8px 4px; letter-spacing:.5px;
  }}
  .plan-tag .cls {{ font-weight:400; color:#999; }}
  .card {{
    background:#fff; border-radius:12px; overflow:hidden;
    box-shadow:0 2px 8px rgba(0,0,0,.10);
    width:400px;
  }}
  .bar {{ height:5px; background:#3370FF; }}
  .header {{ padding:12px 16px 2px; font-size:15px; font-weight:700; color:#1F2329; }}
  .sub {{ font-size:11px; color:#8F959E; font-weight:400; margin-top:2px; }}
  .body {{ padding:4px 16px 14px; }}

  .sec {{ font-size:11px; color:#8F959E; font-weight:700; margin:12px 0 6px; letter-spacing:.3px; }}
  .sec:first-of-type {{ margin-top:10px; }}
  .sec .src {{ font-weight:400; color:#B0B6BF; }}

  .nums {{ display:flex; gap:8px; }}
  .numcard {{ flex:1; background:#F7F8FA; border-radius:10px; padding:10px 4px 8px; text-align:center; }}
  .numcard .n {{ font-size:26px; font-weight:800; letter-spacing:-1px; line-height:1.1; }}
  .numcard .n.green {{ color:#1FA01F; }} .numcard .n.blue {{ color:#3370FF; }}
  .numcard .n.orange {{ color:#FF8800; }} .numcard .n.red {{ color:#F54A45; }}
  .numcard .lab {{ font-size:11px; color:#8F959E; margin-top:3px; }}

  .lamps {{ display:flex; gap:8px; margin-top:8px; }}
  .lamp {{ flex:1; display:flex; align-items:center; gap:6px; background:#F7F8FA; border-radius:8px; padding:8px 10px; font-size:12.5px; color:#333; }}
  .lamp .dot {{ width:9px; height:9px; border-radius:50%; flex:none; }}
  .lamp .dot.green {{ background:#34C724; box-shadow:0 0 0 3px rgba(52,199,36,.15); }}
  .lamp .dot.orange {{ background:#FF8800; box-shadow:0 0 0 3px rgba(255,136,0,.15); }}
  .lamp .t {{ font-weight:600; color:#1F2329; }}
  .lamp .go {{ margin-left:auto; color:#3370FF; text-decoration:none; font-size:11px; font-weight:600; white-space:nowrap; }}

  .att {{ background:#FDF3F3; border-radius:8px; padding:8px 10px; margin-top:8px; font-size:12px; color:#D54941; }}

  .chartbox {{ background:#F7F8FA; border-radius:10px; padding:12px 10px 8px; margin-top:8px; }}
  .chartbox .ttl {{ font-size:11.5px; color:#8F959E; margin-bottom:10px; }}
  .chartbox .ttl b {{ color:#1F2329; }}
  .bars {{ display:flex; align-items:flex-end; gap:6px; padding:0 4px; }}
  .col {{ flex:1; display:flex; flex-direction:column; align-items:center; }}
  .col .pct {{ font-size:10.5px; font-weight:700; color:#1F2329; margin-bottom:2px; line-height:1; }}
  .col .pct.over {{ color:#FF8800; }}
  .col .pill {{ width:100%; max-width:30px; border-radius:4px 4px 0 0; background:#3370FF; }}
  .col .pill.over {{ background:#FF8800; }}
  .col .x {{ font-size:10px; color:#8F959E; margin-top:5px; }}
  .col .n {{ font-size:9.5px; color:#B0B6BF; margin-top:1px; font-variant-numeric:tabular-nums; }}
  .sum {{ display:flex; justify-content:center; gap:14px; margin-top:10px; padding-top:8px; border-top:1px dashed #E2E5EA; font-size:12px; color:#333; }}
  .sum b {{ font-size:14px; color:#1F2329; }}

  .btn-row {{ display:flex; gap:8px; margin-top:12px; }}
  .btn {{ display:inline-block; border-radius:6px; padding:7px 14px; font-size:13px; font-weight:500; text-decoration:none; }}
  .btn.primary {{ background:#3370FF; color:#fff; }}
  .note {{ font-size:11px; color:#8F959E; background:#F7F8FA; border-radius:6px; padding:8px 10px; margin-top:10px; }}
</style>
</head>
<body>

<div class="plan-tag">策略 H · 六区数字卡（真实数据）<span class="cls">数字醒目 · 趋势成图 · 逾期/请假不列人数</span></div>
<div class="card">
  <div class="bar"></div>
  <div class="header">📋 周期报表 · 8.28 第{cycle_no}周期 · {MENTOR}团队<span class="sub">{ledger_src}口径 · 数据截至 {as_of} · 组内 {total} 人</span></div>
  <div class="body">

    <div class="sec">① 人员概况</div>
    <div class="nums">
      {numcard(f"{filled}/{total}", "填报", "green")}
      {numcard(str(total), "总人数", "blue")}
      {numcard(str(unfilled), "未按时", "green")}
    </div>
    {att_line}

    <div class="sec">② 目标数量 <span class="src">（{ledger_src}）</span></div>
    <div class="nums">
      {numcard(str(goals[0]), "大目标")}
      {numcard(str(goals[1]), "小目标")}
      {numcard(str(goals[2]), "周期 TODO")}
    </div>

    <div class="sec">③ 完成情况 <span class="src">（{ledger_src}）</span></div>
    <div class="nums">
      {numcard(str(closure[0]), "已闭环", "green")}
      {numcard(str(closure[1]), "进行中", "orange")}
      {numcard(f"{round(filled / total * 100)}%", "填报率", "green")}
    </div>

    <div class="sec">④ 逾期</div>
    <div class="lamps">
      {lamp("green" if not overdue_cnt else "red", "本周期无逾期" if not overdue_cnt else f"逾期 {overdue_cnt} 条")}
    </div>

    <div class="sec">⑤ 请假 <span class="src">（飞书审批 · 近周期）</span></div>
    <div class="lamps">
      {lamp("orange" if g_leaves else "green", "有请假" if g_leaves else "无请假")}
    </div>

    <div class="sec">⑥ 评价概况 <span class="src">（{ledger_src}）</span></div>
    <div class="nums">
      {numcard(f"{eval_.get('avg', '-')}★", "平均", "orange")}
      {numcard(str(eval_.get("n", 0)), "已评", "blue")}
      {numcard(str(len(eval_.get("comments", []))), "评语", "blue")}
    </div>
    <div class="md" style="font-size:12px; color:#666; margin-top:6px; text-align:center;">{bucket}</div>

    <div class="sec">📈 填报趋势（近 {B.TREND_WINDOW} 期 · 不计请假者）</div>
    <div class="chartbox">
      <div class="ttl">每期填报率 <b>填报人数 / 有效人数</b>（有效 = 已入职 − 当日请假 · 请假豁免填报）</div>
      <div class="bars">
        {''.join(bars)}
      </div>
      <div class="sum"><span>填报率 <b>{avg_pct:.0f}%</b></span><span>填报 <b>{tot_f}</b> 人次</span><span>有效 <b>{tot_v}</b> 人次</span></div>
    </div>

    <div class="btn-row">
      {'<a class="btn primary" href="' + TODO_URL + '">打开全量表格（' + str(total) + ' 人 · 目标/评价/假单）</a>' if TODO_URL else '<div class="note">全量表格链接待海豚解析</div>'}
    </div>

    <div class="note">6 区数字全部大号单列；趋势按期成图（百分比 + 填报/有效人数）。逾期、请假只亮状态灯——绿=无、橙=有，人数与名单收进「详情/名单」链接。填报率可 &gt;100%：橙色柱 = 有在假人员仍填报（超额）</div>
  </div>
</div>

</body>
</html>"""

out_html = WS / "六区数字卡-真实数据.html"
out_html.write_text(html, encoding="utf-8")
print(f"[ok] HTML → {out_html}")

# ---------------------------------------------------------------------------
# 3) PNG（node playwright）
# ---------------------------------------------------------------------------
render_js = r'''
const { chromium } = require('playwright');
(async () => {
  const b = await chromium.launch();
  const p = await b.newPage({ viewport: { width: 520, height: 1400 }, deviceScaleFactor: 2 });
  await p.goto('file://' + process.argv[1], { waitUntil: 'networkidle' });
  await p.waitForTimeout(300);
  const el = await p.$('.card');
  const box = await el.boundingBox();
  await el.screenshot({ path: process.argv[2] });
  console.log('PNG', box.width + 'x' + box.height);
  await b.close();
})();
'''
node_mods = "/public/home/wwb/memory-sota-study/node_modules"
p = subprocess.run(
    ["node", "-e", render_js, str(out_html), str(WS / "六区数字卡-真实数据.png")],
    capture_output=True, text=True, timeout=120,
    env={**__import__("os").environ, "NODE_PATH": node_mods})
print(p.stdout.strip() or p.stderr.strip())
